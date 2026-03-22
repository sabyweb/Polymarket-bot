"""
Position tracking for the Polymarket market-making bot.

Tracks cumulative fill exposure per market and per side (Yes / No).
Halts quoting on a side when the position limit is breached and
resumes when the position drops back below a resume threshold.

Positions are persisted to a JSON file so they survive bot restarts.
"""

import json
import logging
import os
from config import MAX_POSITION_USD, RESUME_POSITION_USD
from alerts import alert_position_limit, log_position_update

log = logging.getLogger(__name__)

POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")


class PositionTracker:
    """Tracks positions across all active markets.

    Each market is keyed by its condition_id and stores per-side
    USD exposure plus a halted flag.  State is persisted to disk
    after every change so positions survive restarts.
    """

    def __init__(self) -> None:
        self.positions: dict[str, dict] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────
    def _load(self) -> None:
        """Load positions from disk if the file exists."""
        if not os.path.exists(POSITIONS_FILE):
            return
        try:
            with open(POSITIONS_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.positions = data
                non_zero = sum(
                    1 for p in data.values()
                    if p.get("yes", 0) > 0 or p.get("no", 0) > 0
                )
                log.info(
                    f"Loaded {len(data)} positions from disk "
                    f"({non_zero} with open exposure)"
                )
        except Exception as e:
            log.warning(f"Could not load positions from {POSITIONS_FILE}: {e}")

    def _save(self) -> None:
        """Persist current positions to disk.

        Auto-prunes entries where both sides are below dust threshold
        ($0.05) and neither side is halted, to prevent stale zero-
        position entries from accumulating.
        """
        # Prune dust/zero positions
        dust = 0.05
        to_prune = [
            cid for cid, pos in self.positions.items()
            if (pos.get("yes", 0) <= dust
                and pos.get("no", 0) <= dust
                and pos.get("yes_shares", 0) <= 0
                and pos.get("no_shares", 0) <= 0
                and not pos.get("yes_halted", False)
                and not pos.get("no_halted", False))
        ]
        for cid in to_prune:
            del self.positions[cid]

        try:
            with open(POSITIONS_FILE, "w") as f:
                json.dump(self.positions, f, indent=2)
        except Exception as e:
            log.error(f"Could not save positions to {POSITIONS_FILE}: {e}")

    # ── Public API ────────────────────────────────────────────────────────────
    def register_market(self, condition_id: str, question: str) -> None:
        """Start tracking a new market.

        Args:
            condition_id: Unique market identifier.
            question: Human-readable market name for logging.
        """
        if condition_id not in self.positions:
            self.positions[condition_id] = {
                "yes": 0.0,
                "no": 0.0,
                "yes_shares": 0.0,
                "no_shares": 0.0,
                "yes_avg_price": 0.0,
                "no_avg_price": 0.0,
                "yes_halted": False,
                "no_halted": False,
                "question": question,
            }
            log.debug(f"Registered market: {question[:50]}")
            self._save()

    def remove_market(self, condition_id: str) -> None:
        """Stop tracking a market.

        Args:
            condition_id: Unique market identifier.
        """
        if condition_id in self.positions:
            del self.positions[condition_id]
            self._save()

    def record_fill(
        self, condition_id: str, side: str, shares: float, price: float,
        question: str = "",
    ) -> None:
        """Record a fill and check whether the position limit is breached.

        Tracks both USD exposure (for halt logic) and shares + VWAP
        (for position-based unwind reconciliation).

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".
            shares: Number of shares filled.
            price: Price per share.
            question: Market name (used for auto-registration if needed).
        """
        if condition_id not in self.positions:
            # Auto-register — fills can arrive for markets that were
            # pruned on restart or removed from the active set while
            # their BUY orders were still live on the exchange.
            name = question or f"unknown-{condition_id[:12]}"
            log.warning(f"Auto-registering market on fill: {name}")
            self.register_market(condition_id, name)
        pos = self.positions[condition_id]
        key = side.lower()

        # Update shares and VWAP
        old_shares = pos.get(f"{key}_shares", 0.0)
        old_avg = pos.get(f"{key}_avg_price", 0.0)
        new_shares = old_shares + shares
        if new_shares > 0:
            new_avg = ((old_avg * old_shares) + (price * shares)) / new_shares
        else:
            new_avg = 0.0
        pos[f"{key}_shares"] = new_shares
        pos[f"{key}_avg_price"] = round(new_avg, 6)

        # Keep USD tracking for halt logic
        filled_usd = shares * price
        pos[key] += filled_usd

        log_position_update(pos["question"], pos["yes"], pos["no"])
        self._check_limit(condition_id, key)
        self._save()

    def record_unwind(
        self, condition_id: str, side: str, shares: float, price: float
    ) -> None:
        """Record a position reduction (e.g. from an unwind SELL fill).

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".
            shares: Number of shares unwound.
            price: Price per share.
        """
        if condition_id not in self.positions:
            return
        pos = self.positions[condition_id]
        key = side.lower()

        # Reduce shares
        pos[f"{key}_shares"] = max(0.0, pos.get(f"{key}_shares", 0.0) - shares)
        if pos[f"{key}_shares"] <= 0:
            pos[f"{key}_avg_price"] = 0.0

        # Reduce USD exposure
        unwound_usd = shares * price
        pos[key] = max(0.0, pos[key] - unwound_usd)

        log.info(
            f"Position unwound | {pos['question'][:40]} | "
            f"{key.upper()} reduced by {shares:.2f} shares (${unwound_usd:.2f}) "
            f"to {pos.get(f'{key}_shares', 0):.2f} shares (${pos[key]:.2f})"
        )
        self._check_resume(condition_id, key)
        self._save()

    def can_quote(self, condition_id: str, side: str) -> bool:
        """Check whether we are allowed to place new orders on a side.

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".

        Returns:
            True if quoting is permitted, False if halted.
        """
        if condition_id not in self.positions:
            return True
        return not self.positions[condition_id].get(
            f"{side.lower()}_halted", False
        )

    def get_position(self, condition_id: str, side: str) -> float:
        """Get current position value for one side.

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".

        Returns:
            Position value in USD.
        """
        if condition_id not in self.positions:
            return 0.0
        return self.positions[condition_id].get(side.lower(), 0.0)

    def get_shares(self, condition_id: str, side: str) -> float:
        """Get current share count for one side.

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".

        Returns:
            Number of shares held.
        """
        if condition_id not in self.positions:
            return 0.0
        return self.positions[condition_id].get(f"{side.lower()}_shares", 0.0)

    def get_avg_price(self, condition_id: str, side: str) -> float:
        """Get volume-weighted average price for one side.

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".

        Returns:
            VWAP of the position.
        """
        if condition_id not in self.positions:
            return 0.0
        return self.positions[condition_id].get(f"{side.lower()}_avg_price", 0.0)

    def get_all_positions(self) -> dict[str, dict]:
        """Return a shallow copy of all tracked positions.

        Returns:
            Dict of condition_id -> position data.
        """
        return self.positions.copy()

    def is_halted(self, condition_id: str, side: str) -> bool:
        """Check if a specific side is halted due to position limit.

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".

        Returns:
            True if quoting is halted.
        """
        if condition_id not in self.positions:
            return False
        return self.positions[condition_id].get(
            f"{side.lower()}_halted", False
        )

    def reset_market(self, condition_id: str) -> None:
        """Reset all position data for a market.

        Args:
            condition_id: Unique market identifier.
        """
        if condition_id in self.positions:
            self.positions[condition_id]["yes"] = 0.0
            self.positions[condition_id]["no"] = 0.0
            self.positions[condition_id]["yes_shares"] = 0.0
            self.positions[condition_id]["no_shares"] = 0.0
            self.positions[condition_id]["yes_avg_price"] = 0.0
            self.positions[condition_id]["no_avg_price"] = 0.0
            self.positions[condition_id]["yes_halted"] = False
            self.positions[condition_id]["no_halted"] = False
            log.info(f"Position reset for: {condition_id}")
            self._save()

    # ── Internal ─────────────────────────────────────────────────────────────
    def _check_limit(self, condition_id: str, side: str) -> None:
        """Halt quoting if position exceeds MAX_POSITION_USD."""
        pos = self.positions[condition_id]
        if pos[side] >= MAX_POSITION_USD and not pos[f"{side}_halted"]:
            pos[f"{side}_halted"] = True
            alert_position_limit(pos["question"], side.upper(), pos[side])

    def _check_resume(self, condition_id: str, side: str) -> None:
        """Resume quoting if position drops back below RESUME_POSITION_USD."""
        pos = self.positions[condition_id]
        if pos[f"{side}_halted"] and pos[side] <= RESUME_POSITION_USD:
            pos[f"{side}_halted"] = False
            log.info(
                f"QUOTING RESUMED | {pos['question'][:40]} | "
                f"{side.upper()} back to ${pos[side]:.2f}"
            )

    def print_summary(self) -> None:
        """Log a summary of all tracked positions."""
        if not self.positions:
            log.info("No positions currently tracked.")
            return
        log.info("-- Position Summary --")
        for cid, pos in self.positions.items():
            yes_h = " [HALTED]" if pos["yes_halted"] else ""
            no_h = " [HALTED]" if pos["no_halted"] else ""
            log.info(
                f"  {pos['question'][:45]} | "
                f"YES=${pos['yes']:.2f} ({pos.get('yes_shares', 0):.1f} shares){yes_h} | "
                f"NO=${pos['no']:.2f} ({pos.get('no_shares', 0):.1f} shares){no_h}"
            )
        log.info("-" * 50)
