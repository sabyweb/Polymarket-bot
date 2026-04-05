"""
Reward tracking and daily performance analysis.

Tracks per-market, per-hour statistics to answer:
  - How much reward are we earning per market?
  - Which markets are most profitable?
  - What parameters (spread, order size, time on book) drive reward capture?
  - Where are we losing money (fills, decay, stop-loss)?

Every 24 hours, produces a detailed report logged + sent to Discord.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from config import (
    DISCORD_WEBHOOK_URL,
    REWARD_LOG_INTERVAL_SECS,
)
from database import get_db

log = logging.getLogger(__name__)

TRACKER_FILE = os.path.join(os.path.dirname(__file__), "reward_history.json")
DAILY_REPORT_INTERVAL = 86400  # 24 hours


# ─────────────────────────────────────────────────────────────────────────────
# Per-market stats accumulated over time
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MarketStats:
    """Accumulated stats for a single market since tracking started."""

    condition_id: str = ""
    question: str = ""
    daily_rate: float = 0.0        # Pool reward rate ($/day)
    max_spread: float = 0.0        # Reward window width

    # Time tracking
    first_seen: float = 0.0        # Timestamp when first traded
    total_cycles: int = 0          # Total order cycles run
    cycles_with_orders: int = 0    # Cycles where we had live orders on book
    cycles_both_sides: int = 0     # Cycles with orders on BOTH yes and no
    cycles_in_reward_window: int = 0    # Cycles where at least one order was within reward spread
    cycles_both_in_window: int = 0      # Cycles where BOTH sides were within reward spread
    time_on_book_secs: float = 0.0 # Estimated seconds with orders live

    # Order placement
    orders_placed: int = 0
    orders_cancelled: int = 0
    cancel_reasons: dict = field(default_factory=dict)  # reason → count

    # Fills
    buy_fills: int = 0
    buy_fill_shares: float = 0.0
    buy_fill_usd: float = 0.0
    sell_fills: int = 0
    sell_fill_shares: float = 0.0
    sell_fill_usd: float = 0.0

    # P&L components
    spread_capture_usd: float = 0.0    # Profit from selling above VWAP
    unwind_loss_usd: float = 0.0       # Loss from selling below VWAP
    stop_loss_usd: float = 0.0         # Loss from stop-loss sells

    # Reward estimates (Q-score based)
    est_reward_usd: float = 0.0        # Running estimated reward earnings (Q-score model)
    est_reward_old: float = 0.0        # Legacy flat-share estimate (for comparison)
    actual_reward_usd: float = 0.0     # Actual rewards from API (when available)
    last_actual_query: float = 0.0     # Timestamp of last actual reward query
    reward_snapshots: list = field(default_factory=list)  # hourly snapshots

    # Q-score tracking (per-cycle accumulation)
    total_q_score: float = 0.0         # Our cumulative Q-score contribution
    total_market_q: float = 0.0        # Estimated total market Q-score
    q_score_samples: int = 0           # Number of Q-score measurements

    # Pricing stats
    avg_bid_price: float = 0.0
    avg_ask_price: float = 0.0
    avg_spread_captured: float = 0.0   # Average spread between our bid and ask
    price_samples: int = 0

    # Inventory
    peak_inventory_usd: float = 0.0
    avg_inventory_usd: float = 0.0
    inventory_samples: int = 0

    # Cooldowns & skew
    cooldown_cycles: int = 0       # Cycles where post-fill cooldown was active
    skew_cycles: int = 0           # Cycles where inventory skew was applied

    # M3: Fill quality tracking
    total_slippage: float = 0.0    # Sum of slippage across all fills (>0 = adverse)
    adverse_fills: int = 0         # Number of fills with positive slippage
    favourable_fills: int = 0      # Number of fills with negative slippage

    def time_on_book_hours(self) -> float:
        return self.time_on_book_secs / 3600

    def fill_rate(self) -> float:
        """Fraction of cycles that resulted in fills."""
        if self.total_cycles == 0:
            return 0.0
        return (self.buy_fills + self.sell_fills) / self.total_cycles

    def uptime_pct(self) -> float:
        """Fraction of cycles with live orders."""
        if self.total_cycles == 0:
            return 0.0
        return self.cycles_with_orders / self.total_cycles * 100

    def both_sides_pct(self) -> float:
        """Fraction of cycles quoting both sides (max reward eligibility)."""
        if self.total_cycles == 0:
            return 0.0
        return self.cycles_both_sides / self.total_cycles * 100

    def in_window_pct(self) -> float:
        """Fraction of cycles with at least one order inside the reward spread."""
        if self.total_cycles == 0:
            return 0.0
        return self.cycles_in_reward_window / self.total_cycles * 100

    def both_in_window_pct(self) -> float:
        """Fraction of cycles with BOTH sides inside the reward spread."""
        if self.total_cycles == 0:
            return 0.0
        return self.cycles_both_in_window / self.total_cycles * 100

    def net_pnl(self) -> float:
        """Net P&L from trading (excludes rewards)."""
        return self.spread_capture_usd - self.unwind_loss_usd - self.stop_loss_usd

    def total_pnl(self) -> float:
        """Net P&L including estimated rewards."""
        return self.net_pnl() + self.est_reward_usd

    def roi_pct(self) -> float:
        """Return on capital deployed (buy fill USD)."""
        if self.buy_fill_usd == 0:
            return 0.0
        return self.total_pnl() / self.buy_fill_usd * 100


# ─────────────────────────────────────────────────────────────────────────────
# RewardTracker: central stats collection and reporting
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Q-Score Calculation (matches Polymarket's actual formula)
# ─────────────────────────────────────────────────────────────────────────────

# Single-sided penalty factor: single-sided orders score at 1/c of two-sided.
# Polymarket uses c = 3.0 as of 2024.
SINGLE_SIDE_PENALTY: float = 3.0


def q_score_order(
    max_spread: float, distance_from_mid: float, size_shares: float,
) -> float:
    """Calculate Q-score for a single order using Polymarket's formula.

    S(v, s) = ((v - s) / v)² × b

    Args:
        max_spread: Maximum reward spread from midpoint (e.g. 0.04 for 4c).
        distance_from_mid: |our_price - midpoint| in price units.
        size_shares: Order size in shares.

    Returns:
        Q-score contribution for this order.  0 if outside reward window.
    """
    if max_spread <= 0 or distance_from_mid >= max_spread or size_shares <= 0:
        return 0.0
    ratio = (max_spread - distance_from_mid) / max_spread
    return ratio * ratio * size_shares


def estimate_market_q(
    order_book: dict, max_spread: float, midpoint: float,
) -> float:
    """Estimate total market Q-score from the visible order book.

    Sums Q-score for all bids and asks within the reward window.
    This is a lower bound (some makers may have hidden orders),
    but it's the best we can do without privileged data.
    """
    total_q = 0.0
    for level in order_book.get("bids", []):
        price = float(level.get("price", 0))
        size = float(level.get("size", 0))
        dist = abs(price - midpoint)
        total_q += q_score_order(max_spread, dist, size)
    for level in order_book.get("asks", []):
        price = float(level.get("price", 0))
        size = float(level.get("size", 0))
        dist = abs(price - midpoint)
        total_q += q_score_order(max_spread, dist, size)
    return total_q


class RewardTracker:
    """Collects per-market performance data and generates periodic reports."""

    def __init__(self) -> None:
        self.markets: dict[str, MarketStats] = {}  # condition_id → stats
        self._bot_start: float = time.time()
        self._last_hourly_log: float = time.time()
        self._last_daily_report: float = time.time()
        self._load()

    # ── Per-cycle recording ──────────────────────────────────────────────────

    def get_or_create(self, condition_id: str, question: str = "",
                      daily_rate: float = 0.0, max_spread: float = 0.0) -> MarketStats:
        """Get or create stats entry for a market."""
        if condition_id not in self.markets:
            self.markets[condition_id] = MarketStats(
                condition_id=condition_id,
                question=question,
                daily_rate=daily_rate,
                max_spread=max_spread,
                first_seen=time.time(),
            )
        stats = self.markets[condition_id]
        # Update metadata that may change
        if question:
            stats.question = question
        if daily_rate > 0:
            stats.daily_rate = daily_rate
        if max_spread > 0:
            stats.max_spread = max_spread
        return stats

    def record_cycle(
        self,
        condition_id: str,
        has_yes_order: bool,
        has_no_order: bool,
        bid_price: float = 0.0,
        ask_price: float = 0.0,
        inventory_usd: float = 0.0,
        cooldown_active: bool = False,
        skew_active: bool = False,
        cycle_duration_secs: float = 30.0,
        midpoint: float = 0.0,
        bid_size: float = 0.0,
        ask_size: float = 0.0,
        order_book: dict | None = None,
    ) -> None:
        """Record stats for one order cycle on a market.

        Now includes Q-score calculation using Polymarket's actual formula.
        """
        stats = self.markets.get(condition_id)
        if not stats:
            return

        stats.total_cycles += 1

        if has_yes_order or has_no_order:
            stats.cycles_with_orders += 1
            stats.time_on_book_secs += cycle_duration_secs

        if has_yes_order and has_no_order:
            stats.cycles_both_sides += 1

        # Track orders within the reward spread window
        if stats.max_spread > 0 and midpoint > 0:
            yes_in = has_yes_order and bid_price > 0 and abs(bid_price - midpoint) <= stats.max_spread
            no_in = has_no_order and ask_price > 0 and abs(ask_price - midpoint) <= stats.max_spread
            if yes_in or no_in:
                stats.cycles_in_reward_window += 1
            if yes_in and no_in:
                stats.cycles_both_in_window += 1

        if bid_price > 0 and ask_price > 0:
            stats.price_samples += 1
            # Running average
            n = stats.price_samples
            stats.avg_bid_price = stats.avg_bid_price * (n - 1) / n + bid_price / n
            stats.avg_ask_price = stats.avg_ask_price * (n - 1) / n + ask_price / n
            stats.avg_spread_captured = stats.avg_ask_price - stats.avg_bid_price

        if inventory_usd > 0:
            stats.inventory_samples += 1
            n = stats.inventory_samples
            stats.avg_inventory_usd = (
                stats.avg_inventory_usd * (n - 1) / n + inventory_usd / n
            )
            stats.peak_inventory_usd = max(stats.peak_inventory_usd, inventory_usd)

        if cooldown_active:
            stats.cooldown_cycles += 1
        if skew_active:
            stats.skew_cycles += 1

        # ── Q-Score calculation ───────────────────────────────────────
        # Calculate our Q-score for this cycle based on actual order
        # positions relative to midpoint.  This is far more accurate
        # than the old flat 10% assumption.
        if stats.max_spread > 0 and midpoint > 0:
            our_q = 0.0
            if bid_price > 0 and bid_size > 0:
                bid_dist = abs(bid_price - midpoint)
                our_q += q_score_order(stats.max_spread, bid_dist, bid_size)
            if ask_price > 0 and ask_size > 0:
                ask_dist = abs(ask_price - midpoint)
                our_q += q_score_order(stats.max_spread, ask_dist, ask_size)

            # Apply single-sided penalty (Polymarket penalizes 1/3)
            if (has_yes_order and not has_no_order) or (has_no_order and not has_yes_order):
                our_q /= SINGLE_SIDE_PENALTY

            # Estimate total market Q from visible order book
            market_q = 0.0
            if order_book:
                market_q = estimate_market_q(
                    order_book, stats.max_spread, midpoint
                )

            if our_q > 0:
                stats.total_q_score += our_q
                stats.total_market_q += max(market_q, our_q)
                stats.q_score_samples += 1

    def record_order_placed(self, condition_id: str) -> None:
        stats = self.markets.get(condition_id)
        if stats:
            stats.orders_placed += 1

    def record_order_cancelled(self, condition_id: str, reason: str = "") -> None:
        stats = self.markets.get(condition_id)
        if stats:
            stats.orders_cancelled += 1
            if reason:
                stats.cancel_reasons[reason] = stats.cancel_reasons.get(reason, 0) + 1

    def record_buy_fill(self, condition_id: str, shares: float, usd: float) -> None:
        stats = self.markets.get(condition_id)
        if stats:
            stats.buy_fills += 1
            stats.buy_fill_shares += shares
            stats.buy_fill_usd += usd

    def record_fill_quality(
        self, condition_id: str, slippage: float,
    ) -> None:
        """Record fill quality metric for M3 tracking.

        Args:
            condition_id: Market condition ID.
            slippage: Fill price minus midpoint. Positive = adverse.
        """
        stats = self.markets.get(condition_id)
        if not stats:
            return
        stats.total_slippage += slippage
        if slippage > 0:
            stats.adverse_fills += 1
        else:
            stats.favourable_fills += 1

    def record_sell_fill(self, condition_id: str, shares: float, usd: float,
                         vwap_cost_usd: float = 0.0) -> None:
        """Record a sell fill. Computes spread capture or unwind loss."""
        stats = self.markets.get(condition_id)
        if not stats:
            return
        stats.sell_fills += 1
        stats.sell_fill_shares += shares
        stats.sell_fill_usd += usd

        if vwap_cost_usd > 0:
            pnl = usd - vwap_cost_usd
            if pnl >= 0:
                stats.spread_capture_usd += pnl
            else:
                stats.unwind_loss_usd += abs(pnl)

    def record_stop_loss(self, condition_id: str, loss_usd: float) -> None:
        stats = self.markets.get(condition_id)
        if stats:
            stats.stop_loss_usd += loss_usd

    def record_reward_estimate(self, condition_id: str, est_hourly: float) -> None:
        """Record an hourly reward estimate snapshot."""
        stats = self.markets.get(condition_id)
        if stats:
            stats.est_reward_usd += est_hourly
            stats.reward_snapshots.append({
                "timestamp": time.time(),
                "est_hourly": est_hourly,
            })
            # Keep only last 48 hours of snapshots
            cutoff = time.time() - 48 * 3600
            stats.reward_snapshots = [
                s for s in stats.reward_snapshots if s["timestamp"] > cutoff
            ]

    def record_actual_rewards(self, total_earned: float,
                              per_market: dict[str, float] | None = None) -> None:
        """Record actual rewards queried from the API.

        Args:
            total_earned: Total rewards earned across all markets.
            per_market: Optional {condition_id: earned_amount} breakdown.
        """
        if per_market:
            for cid, earned in per_market.items():
                stats = self.markets.get(cid)
                if stats:
                    stats.actual_reward_usd = earned
                    stats.last_actual_query = time.time()

        # Log aggregate comparison to database
        total_est = sum(s.est_reward_usd for s in self.markets.values())
        total_old = sum(s.est_reward_old for s in self.markets.values())
        total_q = sum(s.total_q_score for s in self.markets.values())
        total_mkt_q = sum(s.total_market_q for s in self.markets.values())
        q_share = total_q / total_mkt_q if total_mkt_q > 0 else 0

        get_db().log_reward_comparison(
            condition_id="",  # aggregate
            q_score_est=total_est, legacy_est=total_old,
            actual_earned=total_earned, q_share_pct=q_share,
            our_q_score=total_q, market_q_score=total_mkt_q,
        )
        # Per-market comparisons
        if per_market:
            for cid, earned in per_market.items():
                stats = self.markets.get(cid)
                if stats:
                    mq = stats.total_market_q
                    qs = stats.total_q_score / mq if mq > 0 else 0
                    get_db().log_reward_comparison(
                        condition_id=cid,
                        q_score_est=stats.est_reward_usd,
                        legacy_est=stats.est_reward_old,
                        actual_earned=earned, q_share_pct=qs,
                        our_q_score=stats.total_q_score,
                        market_q_score=mq,
                    )

        if total_earned > 0 and total_est > 0:
            variance_pct = (total_est - total_earned) / total_earned * 100
            log.info(
                f"REWARD VARIANCE | estimated=${total_est:.2f} | "
                f"actual=${total_earned:.2f} | "
                f"variance={variance_pct:+.1f}%"
            )
            if abs(variance_pct) > 30:
                log.warning(
                    f"Reward estimate is {abs(variance_pct):.0f}% off from actual — "
                    f"model may need recalibration"
                )

            # Per-market comparison if available
            if per_market:
                for cid, actual in per_market.items():
                    stats = self.markets.get(cid)
                    if stats and actual > 0:
                        est = stats.est_reward_usd
                        var = (est - actual) / actual * 100 if actual > 0 else 0
                        log.info(
                            f"  {stats.question[:40]:<40} | "
                            f"est=${est:.2f} | actual=${actual:.2f} | "
                            f"Δ={var:+.1f}%"
                        )

    def get_reward_accuracy(self) -> dict:
        """Return accuracy metrics comparing Q-score vs legacy vs actual.

        Useful for analyzing which model is more accurate.
        """
        result = {
            "q_score_total": sum(s.est_reward_usd for s in self.markets.values()),
            "legacy_total": sum(s.est_reward_old for s in self.markets.values()),
            "actual_total": sum(s.actual_reward_usd for s in self.markets.values()),
            "per_market": {},
        }
        for cid, stats in self.markets.items():
            if stats.actual_reward_usd > 0:
                q_err = abs(stats.est_reward_usd - stats.actual_reward_usd)
                old_err = abs(stats.est_reward_old - stats.actual_reward_usd)
                result["per_market"][cid] = {
                    "question": stats.question[:40],
                    "q_score_est": stats.est_reward_usd,
                    "legacy_est": stats.est_reward_old,
                    "actual": stats.actual_reward_usd,
                    "q_score_error": q_err,
                    "legacy_error": old_err,
                    "q_score_better": q_err < old_err,
                }
        return result

    # ── Hourly logging ───────────────────────────────────────────────────────

    def maybe_log_hourly(self, active_markets: list[dict]) -> bool:
        """Log per-market hourly stats if interval has elapsed.

        Returns True if a log was produced.
        """
        now = time.time()
        if now - self._last_hourly_log < REWARD_LOG_INTERVAL_SECS:
            return False

        self._last_hourly_log = now
        self._log_hourly_report(active_markets)
        self._save()
        return True

    def _log_hourly_report(self, active_markets: list[dict]) -> None:
        """Log per-market reward estimates using Q-score model + legacy comparison."""
        log.info("=" * 70)
        log.info("HOURLY REWARD REPORT")
        log.info("=" * 70)

        total_q_hourly = 0.0
        total_q_daily = 0.0
        total_old_hourly = 0.0
        total_old_daily = 0.0

        for m in active_markets:
            cid = m.get("condition_id", "")
            stats = self.markets.get(cid)
            if not stats:
                continue

            pool_daily = stats.daily_rate
            uptime = stats.uptime_pct() / 100
            both_sides = stats.both_sides_pct() / 100

            # ── Q-Score model (accurate) ──────────────────────────────
            # Our share = our_Q / total_market_Q (measured from order book)
            q_share_pct = 0.0
            if stats.total_market_q > 0 and stats.q_score_samples > 0:
                q_share_pct = stats.total_q_score / stats.total_market_q
            q_est_hourly = pool_daily * q_share_pct * uptime / 24
            q_est_daily = pool_daily * q_share_pct * uptime

            total_q_hourly += q_est_hourly
            total_q_daily += q_est_daily

            # Record Q-score estimate
            self.record_reward_estimate(cid, q_est_hourly)

            # ── Legacy flat-share model (for comparison) ──────────────
            old_share = 0.10 * uptime * (0.5 + 0.5 * both_sides)
            old_hourly = pool_daily * old_share / 24
            old_daily = pool_daily * old_share
            total_old_hourly += old_hourly
            total_old_daily += old_daily
            stats.est_reward_old += old_hourly

            # ── Variance logging ──────────────────────────────────────
            variance = ""
            if old_hourly > 0:
                diff_pct = (q_est_hourly - old_hourly) / old_hourly * 100
                variance = f" | Δ={diff_pct:+.0f}%"

            log.info(
                f"  {stats.question[:40]:<40} | "
                f"pool=${pool_daily:>6.0f}/d | "
                f"Q=${q_est_hourly:>5.2f}/hr (${q_est_daily:>5.2f}/d) | "
                f"old=${old_hourly:>5.2f}/hr | "
                f"Q_share={q_share_pct:>5.1%} | "
                f"uptime={stats.uptime_pct():>4.0f}% | "
                f"both={stats.both_sides_pct():>4.0f}% | "
                f"in_window={stats.in_window_pct():>4.0f}%{variance}"
            )

        log.info(
            f"  {'TOTAL (Q-score)':<40} | "
            f"{'':>13} | "
            f"Q=${total_q_hourly:>5.2f}/hr (${total_q_daily:>5.2f}/d) | "
            f"old=${total_old_hourly:>5.2f}/hr"
        )
        if total_old_hourly > 0:
            total_diff = (total_q_hourly - total_old_hourly) / total_old_hourly * 100
            log.info(
                f"  Model comparison: Q-score is {total_diff:+.1f}% vs flat 10% model"
            )
        log.info("=" * 70)

    # ── Daily report ─────────────────────────────────────────────────────────

    def maybe_generate_daily_report(self) -> bool:
        """Generate a comprehensive daily report if 24h have elapsed.

        Returns True if a report was generated.
        """
        now = time.time()
        if now - self._last_daily_report < DAILY_REPORT_INTERVAL:
            return False

        self._last_daily_report = now
        self._generate_daily_report()
        self._save()
        return True

    def _generate_daily_report(self) -> None:
        """Produce a detailed 24-hour performance analysis."""
        runtime_hours = (time.time() - self._bot_start) / 3600

        log.info("")
        log.info("=" * 80)
        log.info("  24-HOUR DAILY PERFORMANCE REPORT")
        log.info(f"  Bot runtime: {runtime_hours:.1f} hours")
        log.info("=" * 80)

        if not self.markets:
            log.info("  No market data to report.")
            return

        # Sort markets by estimated total P&L (best first)
        sorted_markets = sorted(
            self.markets.values(),
            key=lambda s: s.total_pnl(),
            reverse=True,
        )

        # ── Section 1: Market Rankings ────────────────────────────────────
        log.info("")
        log.info("─── MARKET RANKINGS (by total P&L) ───")
        log.info(f"  {'#':<3} {'Market':<45} {'Reward$':>8} {'Trade$':>8} "
                 f"{'Total$':>8} {'ROI%':>7} {'Uptime':>7} {'Both%':>7} {'InWin%':>7}")
        log.info("  " + "─" * 95)

        total_rewards = 0.0
        total_trade_pnl = 0.0
        total_buy_usd = 0.0

        for i, stats in enumerate(sorted_markets, 1):
            reward_est = stats.est_reward_usd
            trade_pnl = stats.net_pnl()
            total_p = stats.total_pnl()
            total_rewards += reward_est
            total_trade_pnl += trade_pnl
            total_buy_usd += stats.buy_fill_usd

            log.info(
                f"  {i:<3} {stats.question[:45]:<45} "
                f"${reward_est:>7.2f} ${trade_pnl:>7.2f} "
                f"${total_p:>7.2f} {stats.roi_pct():>6.1f}% "
                f"{stats.uptime_pct():>6.1f}% {stats.both_sides_pct():>6.1f}% "
                f"{stats.both_in_window_pct():>6.1f}%"
            )

        log.info("  " + "─" * 95)
        total_roi = (total_rewards + total_trade_pnl) / total_buy_usd * 100 if total_buy_usd > 0 else 0
        log.info(
            f"  {'':3} {'TOTALS':<45} "
            f"${total_rewards:>7.2f} ${total_trade_pnl:>7.2f} "
            f"${total_rewards + total_trade_pnl:>7.2f} {total_roi:>6.1f}%"
        )

        # ── Section 2: Fill Analysis ──────────────────────────────────────
        log.info("")
        log.info("─── FILL ANALYSIS ───")
        log.info(f"  {'Market':<45} {'Buys':>6} {'Buy$':>8} {'Sells':>6} "
                 f"{'Sell$':>8} {'Spread$':>8} {'Loss$':>8} {'StopL$':>8}")
        log.info("  " + "─" * 100)

        for stats in sorted_markets:
            if stats.buy_fills == 0 and stats.sell_fills == 0:
                continue
            log.info(
                f"  {stats.question[:45]:<45} "
                f"{stats.buy_fills:>6} ${stats.buy_fill_usd:>7.2f} "
                f"{stats.sell_fills:>6} ${stats.sell_fill_usd:>7.2f} "
                f"${stats.spread_capture_usd:>7.2f} "
                f"${stats.unwind_loss_usd:>7.2f} "
                f"${stats.stop_loss_usd:>7.2f}"
            )

        # ── Section 3: Order Efficiency ───────────────────────────────────
        log.info("")
        log.info("─── ORDER EFFICIENCY ───")
        log.info(f"  {'Market':<45} {'Placed':>7} {'Cancel':>7} "
                 f"{'Fill%':>7} {'AvgBid':>7} {'AvgAsk':>7} {'Spread':>7}")
        log.info("  " + "─" * 88)

        for stats in sorted_markets:
            if stats.total_cycles == 0:
                continue
            log.info(
                f"  {stats.question[:45]:<45} "
                f"{stats.orders_placed:>7} {stats.orders_cancelled:>7} "
                f"{stats.fill_rate() * 100:>6.1f}% "
                f"{stats.avg_bid_price:>6.4f} {stats.avg_ask_price:>6.4f} "
                f"{stats.avg_spread_captured:>6.4f}"
            )

        # ── Section 4: Cancel Reason Breakdown ────────────────────────────
        log.info("")
        log.info("─── CANCEL REASONS (across all markets) ───")
        all_reasons: dict[str, int] = {}
        for stats in sorted_markets:
            for reason, count in stats.cancel_reasons.items():
                all_reasons[reason] = all_reasons.get(reason, 0) + count
        for reason, count in sorted(all_reasons.items(), key=lambda x: -x[1]):
            log.info(f"  {reason:<30} {count:>6}")

        # ── Section 5: Inventory & Risk ───────────────────────────────────
        log.info("")
        log.info("─── INVENTORY & RISK ───")
        log.info(f"  {'Market':<45} {'PeakInv$':>9} {'AvgInv$':>9} "
                 f"{'Cooldowns':>10} {'SkewCyc':>8}")
        log.info("  " + "─" * 85)

        for stats in sorted_markets:
            if stats.peak_inventory_usd == 0 and stats.cooldown_cycles == 0:
                continue
            log.info(
                f"  {stats.question[:45]:<45} "
                f"${stats.peak_inventory_usd:>8.2f} ${stats.avg_inventory_usd:>8.2f} "
                f"{stats.cooldown_cycles:>10} {stats.skew_cycles:>8}"
            )

        # ── Section 6: Best/Worst Parameters ─────────────────────────────
        log.info("")
        log.info("─── KEY INSIGHTS ───")

        # Best reward rate
        if sorted_markets:
            best_reward = max(sorted_markets, key=lambda s: s.est_reward_usd)
            worst_reward = min(sorted_markets, key=lambda s: s.est_reward_usd)
            best_uptime = max(sorted_markets, key=lambda s: s.uptime_pct())
            most_fills = max(sorted_markets, key=lambda s: s.buy_fills + s.sell_fills)
            biggest_loss = min(sorted_markets, key=lambda s: s.net_pnl())

            log.info(f"  Best reward earner:  {best_reward.question[:45]} (${best_reward.est_reward_usd:.2f})")
            log.info(f"  Worst reward earner: {worst_reward.question[:45]} (${worst_reward.est_reward_usd:.2f})")
            log.info(f"  Highest uptime:      {best_uptime.question[:45]} ({best_uptime.uptime_pct():.1f}%)")
            log.info(f"  Most fills:          {most_fills.question[:45]} ({most_fills.buy_fills}B/{most_fills.sell_fills}S)")
            log.info(f"  Biggest trade loss:  {biggest_loss.question[:45]} (${biggest_loss.net_pnl():.2f})")

        # Reward rate efficiency: est_reward / pool_daily
        log.info("")
        log.info("  Reward capture efficiency by pool size:")
        for stats in sorted(sorted_markets, key=lambda s: s.daily_rate, reverse=True):
            if stats.daily_rate > 0 and stats.est_reward_usd > 0:
                capture_rate = stats.est_reward_usd / (
                    stats.daily_rate * runtime_hours / 24
                ) * 100 if runtime_hours > 0 else 0
                log.info(
                    f"    {stats.question[:45]} | "
                    f"pool=${stats.daily_rate:.0f}/day | "
                    f"captured={capture_rate:.1f}% of pool"
                )

        # ── Section 7: Model Accuracy Comparison ──────────────────────
        accuracy = self.get_reward_accuracy()
        if accuracy["actual_total"] > 0:
            log.info("")
            log.info("─── REWARD MODEL ACCURACY (vs actual) ───")
            q_total = accuracy["q_score_total"]
            old_total = accuracy["legacy_total"]
            actual = accuracy["actual_total"]
            q_err_pct = (q_total - actual) / actual * 100 if actual else 0
            old_err_pct = (old_total - actual) / actual * 100 if actual else 0
            log.info(
                f"  Q-Score model:  ${q_total:>8.2f} (Δ={q_err_pct:+.1f}% from actual)"
            )
            log.info(
                f"  Legacy model:   ${old_total:>8.2f} (Δ={old_err_pct:+.1f}% from actual)"
            )
            log.info(
                f"  Actual earned:  ${actual:>8.2f}"
            )
            better = "Q-Score" if abs(q_err_pct) < abs(old_err_pct) else "Legacy"
            log.info(f"  Winner: {better} model")

            for cid, data in accuracy["per_market"].items():
                marker = "✓" if data["q_score_better"] else "✗"
                log.info(
                    f"    {marker} {data['question']:<40} | "
                    f"Q=${data['q_score_est']:.2f} | "
                    f"old=${data['legacy_est']:.2f} | "
                    f"actual=${data['actual']:.2f}"
                )

        # ── Section 8: Fill Quality (M3) ────────────────────────────────
        fills_with_quality = [
            s for s in sorted_markets
            if s.adverse_fills + s.favourable_fills > 0
        ]
        if fills_with_quality:
            log.info("")
            log.info("─── FILL QUALITY (M3) ───")
            log.info(f"  {'Market':<45} {'Fills':>6} {'Adv':>5} {'Fav':>5} "
                     f"{'Adv%':>6} {'AvgSlip':>9}")
            log.info("  " + "─" * 80)

            total_adv = 0
            total_fav = 0
            total_slip = 0.0
            total_qual_fills = 0
            for stats in fills_with_quality:
                n_fills = stats.adverse_fills + stats.favourable_fills
                total_adv += stats.adverse_fills
                total_fav += stats.favourable_fills
                total_slip += stats.total_slippage
                total_qual_fills += n_fills
                avg_slip = stats.total_slippage / n_fills if n_fills > 0 else 0
                adv_pct = stats.adverse_fills / n_fills * 100 if n_fills > 0 else 0
                log.info(
                    f"  {stats.question[:45]:<45} "
                    f"{n_fills:>6} {stats.adverse_fills:>5} "
                    f"{stats.favourable_fills:>5} "
                    f"{adv_pct:>5.1f}% "
                    f"{avg_slip:>+8.4f}"
                )

            if total_qual_fills > 0:
                agg_adv_pct = total_adv / total_qual_fills * 100
                agg_avg_slip = total_slip / total_qual_fills
                log.info("  " + "─" * 80)
                log.info(
                    f"  {'TOTALS':<45} "
                    f"{total_qual_fills:>6} {total_adv:>5} {total_fav:>5} "
                    f"{agg_adv_pct:>5.1f}% {agg_avg_slip:>+8.4f}"
                )
                if agg_adv_pct > 60:
                    log.warning(
                        f"  ⚠ High adverse fill rate ({agg_adv_pct:.0f}%) — "
                        f"consider widening spreads or reducing size"
                    )

        log.info("")
        log.info("=" * 80)
        log.info("  END OF DAILY REPORT")
        log.info("=" * 80)
        log.info("")

        # ── Send summary to Discord ──────────────────────────────────────
        self._send_daily_discord(sorted_markets, total_rewards,
                                 total_trade_pnl, runtime_hours)

    def _send_daily_discord(self, sorted_markets: list[MarketStats],
                            total_rewards: float, total_trade_pnl: float,
                            runtime_hours: float) -> None:
        """Send a condensed daily report to Discord."""
        try:
            from alerts import _send_discord

            lines = []
            for i, s in enumerate(sorted_markets[:5], 1):
                lines.append(
                    f"**{i}. {s.question[:40]}**\n"
                    f"   Rewards: ${s.est_reward_usd:.2f} | "
                    f"Trade P&L: ${s.net_pnl():.2f} | "
                    f"Uptime: {s.uptime_pct():.0f}% | "
                    f"InWin: {s.both_in_window_pct():.0f}% | "
                    f"Fills: {s.buy_fills}B/{s.sell_fills}S"
                )

            total_pnl = total_rewards + total_trade_pnl
            description = (
                f"**Runtime:** {runtime_hours:.1f} hours\n"
                f"**Est. Rewards:** ${total_rewards:.2f}\n"
                f"**Trade P&L:** ${total_trade_pnl:.2f}\n"
                f"**Net P&L:** ${total_pnl:.2f}\n\n"
                + "\n\n".join(lines)
            )

            _send_discord(
                "Daily Performance Report",
                {
                    "title": "📊 24-Hour Performance Report",
                    "description": description,
                    "color": 0x2ECC71 if total_pnl >= 0 else 0xE74C3C,
                },
            )
        except Exception as e:
            log.debug(f"Could not send daily report to Discord: {e}")

    # ── Persistence (A3: SQLite primary, JSON fallback for migration) ──────

    def _save(self) -> None:
        """Save tracker state to SQLite."""
        try:
            db = get_db()
            db.save_reward_state(
                self._bot_start, self._last_hourly_log, self._last_daily_report,
            )
            db.save_all_reward_stats(self.markets)
        except Exception as e:
            log.warning(f"Could not save reward tracker: {e}")

    def _load(self) -> None:
        """Load tracker state: try SQLite first, fall back to JSON for migration."""
        db = get_db()

        # Try SQLite first
        state = db.load_reward_state()
        stats_data = db.load_all_reward_stats()

        if state or stats_data:
            if state:
                self._bot_start = state.get("bot_start", self._bot_start)
                self._last_hourly_log = state.get("last_hourly_log", self._last_hourly_log)
                self._last_daily_report = state.get("last_daily_report", self._last_daily_report)
            for cid, d in stats_data.items():
                stats = MarketStats()
                for key, val in d.items():
                    if hasattr(stats, key):
                        setattr(stats, key, val)
                self.markets[cid] = stats
            log.info(f"Loaded reward tracker: {len(self.markets)} markets from SQLite")
            return

        # Fall back to JSON (migration path)
        if not os.path.exists(TRACKER_FILE):
            return
        try:
            with open(TRACKER_FILE, "r") as f:
                data = json.load(f)

            self._bot_start = data.get("bot_start", self._bot_start)
            self._last_hourly_log = data.get("last_hourly_log", self._last_hourly_log)
            self._last_daily_report = data.get("last_daily_report", self._last_daily_report)

            for cid, d in data.get("markets", {}).items():
                stats = MarketStats()
                for key, val in d.items():
                    if hasattr(stats, key):
                        setattr(stats, key, val)
                self.markets[cid] = stats

            log.info(
                f"Migrated reward tracker: {len(self.markets)} markets "
                f"from JSON to SQLite"
            )
            # Save to SQLite immediately
            self._save()
            # Rename old JSON so it doesn't get loaded again
            try:
                migrated = TRACKER_FILE + ".migrated"
                os.rename(TRACKER_FILE, migrated)
                log.info(f"Renamed {TRACKER_FILE} → {migrated}")
            except OSError as rename_err:
                log.warning(f"Could not rename reward_history.json: {rename_err}")

        except Exception as e:
            log.warning(f"Could not load reward tracker: {e}")

    # ── Manual report trigger ────────────────────────────────────────────────

    def force_daily_report(self) -> None:
        """Generate daily report immediately (for testing or manual trigger)."""
        self._generate_daily_report()
        self._save()

    def get_summary(self) -> dict[str, Any]:
        """Return a summary dict for programmatic access."""
        runtime_hours = (time.time() - self._bot_start) / 3600
        return {
            "runtime_hours": runtime_hours,
            "markets_tracked": len(self.markets),
            "total_est_rewards": sum(s.est_reward_usd for s in self.markets.values()),
            "total_trade_pnl": sum(s.net_pnl() for s in self.markets.values()),
            "total_buy_usd": sum(s.buy_fill_usd for s in self.markets.values()),
            "total_sell_usd": sum(s.sell_fill_usd for s in self.markets.values()),
            "per_market": {
                cid: {
                    "question": s.question[:50],
                    "est_rewards": s.est_reward_usd,
                    "trade_pnl": s.net_pnl(),
                    "uptime_pct": s.uptime_pct(),
                    "in_window_pct": s.both_in_window_pct(),
                    "fills": s.buy_fills + s.sell_fills,
                }
                for cid, s in self.markets.items()
            },
        }
