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
import tempfile
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
        """Persist current positions to disk atomically, pruning zero entries.

        Uses write-to-temp-file + rename to prevent data corruption if the
        process is killed mid-write (e.g. OOM kill, power loss).
        """
        try:
            # Prune entries where both sides are zero (no position, no halt)
            pruned = {
                cid: pos for cid, pos in self.positions.items()
                if (
                    pos.get("yes", 0) > 0.01
                    or pos.get("no", 0) > 0.01
                    or pos.get("yes_shares", 0) >= 1.0
                    or pos.get("no_shares", 0) >= 1.0
                    or pos.get("yes_halted", False)
                    or pos.get("no_halted", False)
                )
            }
            # Atomic write: write to temp file, then rename (rename is atomic on POSIX)
            dir_name = os.path.dirname(POSITIONS_FILE) or "."
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(pruned, f, indent=2)
                os.replace(tmp_path, POSITIONS_FILE)
            except Exception:
                # Clean up temp file if rename failed
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
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

        # Keep USD tracking for halt logic.
        # price is stored as YES-equivalent for VWAP, but USD exposure
        # should reflect actual CLOB cost: YES = price, NO = 1 - price.
        clob_cost = price if key == "yes" else (1 - price)
        filled_usd = shares * clob_cost
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

        # Reduce USD exposure.
        # price is YES-equivalent (same convention as record_fill).
        # Use actual CLOB cost for consistent USD tracking.
        clob_cost = price if key == "yes" else (1 - price)
        unwound_usd = shares * clob_cost
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

    def recalculate_usd(self) -> None:
        """Recalculate USD exposure and re-check all position limits.

        Fixes legacy data where NO-side USD was incorrectly computed
        using YES-equivalent price instead of actual CLOB cost.
        Also ensures halt flags are consistent with current USD values
        (catches cases where positions.json was loaded with stale halts).
        Called once on startup.
        """
        changed = False
        for cid, pos in self.positions.items():
            for key in ("yes", "no"):
                shares = pos.get(f"{key}_shares", 0.0)
                avg = pos.get(f"{key}_avg_price", 0.0)
                if shares < 1.0 or avg <= 0:
                    continue
                # avg_price is YES-equivalent; actual CLOB cost:
                clob_cost = avg if key == "yes" else (1 - avg)
                correct_usd = round(shares * clob_cost, 2)
                current_usd = pos.get(key, 0.0)
                if abs(correct_usd - current_usd) > 1.0:
                    log.warning(
                        f"USD CORRECTION | {pos.get('question', cid[:16])[:40]} | "
                        f"{key.upper()} | was=${current_usd:.2f} → "
                        f"corrected=${correct_usd:.2f} "
                        f"({shares:.1f} shares × {clob_cost:.4f})"
                    )
                    pos[key] = correct_usd
                    changed = True

                # ALWAYS re-check limits — catches stale halt flags
                # loaded from positions.json (e.g. halt never set because
                # _check_limit only runs on new fills, not on load).
                self._check_limit(cid, key)
                self._check_resume(cid, key)
        if changed:
            self._save()

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

    def reset_side(self, condition_id: str, side: str) -> None:
        """Reset position data for one side of a market.

        Used when exchange verification shows stale data (e.g. user
        manually closed a position outside the bot).

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".
        """
        if condition_id not in self.positions:
            return
        key = side.lower()
        pos = self.positions[condition_id]
        pos[key] = 0.0
        pos[f"{key}_shares"] = 0.0
        pos[f"{key}_avg_price"] = 0.0
        pos[f"{key}_halted"] = False
        question = pos.get("question", condition_id[:16])
        log.info(f"Position side reset | {question[:40]} | {side.upper()} zeroed")
        self._save()

    def set_shares(self, condition_id: str, side: str, shares: float) -> None:
        """Set share count to a specific value (for exchange corrections).

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".
            shares: Corrected share count from exchange.
        """
        if condition_id not in self.positions:
            return
        key = side.lower()
        pos = self.positions[condition_id]
        old_shares = pos.get(f"{key}_shares", 0.0)
        pos[f"{key}_shares"] = shares
        # Adjust USD proportionally
        if old_shares > 0:
            ratio = shares / old_shares
            pos[key] = pos.get(key, 0.0) * ratio
        question = pos.get("question", condition_id[:16])
        log.info(
            f"Position corrected | {question[:40]} | {side.upper()} "
            f"{old_shares:.2f} → {shares:.2f} shares"
        )
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
