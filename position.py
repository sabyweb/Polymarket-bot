"""
Position tracking for the Polymarket market-making bot.

Tracks cumulative fill exposure per market and per side (Yes / No).
Halts quoting on a side when the position limit is breached and
resumes when the position drops back below a resume threshold.
"""

import logging
from config import MAX_POSITION_USD, RESUME_POSITION_USD
from alerts import alert_position_limit, log_position_update

log = logging.getLogger(__name__)


class PositionTracker:
    """Tracks positions across all active markets.

    Each market is keyed by its condition_id and stores per-side
    USD exposure plus a halted flag.
    """

    def __init__(self) -> None:
        self.positions: dict[str, dict] = {}

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
                "yes_halted": False,
                "no_halted": False,
                "question": question,
            }
            log.debug(f"Registered market: {question[:50]}")

    def remove_market(self, condition_id: str) -> None:
        """Stop tracking a market.

        Args:
            condition_id: Unique market identifier.
        """
        if condition_id in self.positions:
            del self.positions[condition_id]

    def record_fill(
        self, condition_id: str, side: str, filled_usd: float
    ) -> None:
        """Record a fill and check whether the position limit is breached.

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".
            filled_usd: Dollar value of the fill.
        """
        if condition_id not in self.positions:
            log.warning(f"Fill for unregistered market: {condition_id}")
            return
        pos = self.positions[condition_id]
        side = side.lower()
        pos[side] += filled_usd
        log_position_update(pos["question"], pos["yes"], pos["no"])
        self._check_limit(condition_id, side)

    def record_unwind(
        self, condition_id: str, side: str, unwound_usd: float
    ) -> None:
        """Record a position reduction (e.g. from an opposing fill).

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".
            unwound_usd: Dollar value of the reduction.
        """
        if condition_id not in self.positions:
            return
        pos = self.positions[condition_id]
        side = side.lower()
        pos[side] = max(0.0, pos[side] - unwound_usd)
        log.info(
            f"Position unwound | {pos['question'][:40]} | "
            f"{side.upper()} reduced by ${unwound_usd:.2f} to ${pos[side]:.2f}"
        )
        self._check_resume(condition_id, side)

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
            self.positions[condition_id]["yes_halted"] = False
            self.positions[condition_id]["no_halted"] = False
            log.info(f"Position reset for: {condition_id}")

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
                f"YES=${pos['yes']:.2f}{yes_h} | "
                f"NO=${pos['no']:.2f}{no_h}"
            )
        log.info("-" * 50)
