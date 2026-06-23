"""
Core state management for the Polymarket market-making bot.

Replaces position.py with a design where:
  1. USD exposure is ALWAYS derived from (shares * clob_cost), never accumulated.
     This eliminates the entire class of "wrong USD" bugs.
  2. Each SidePosition knows its own side, so clob_cost conversion is automatic.
  3. Halt flags use hysteresis (halt at MAX_POSITION_USD, resume at RESUME_POSITION_USD).
  4. Persistence format is backward-compatible with the old positions.json.

Public API is identical to PositionTracker so migration is a one-line import swap.
"""

import json
import logging
import os
import threading
import time
from config import MAX_POSITION_USD, RESUME_POSITION_USD
from alerts import alert_position_limit, log_position_update

log = logging.getLogger(__name__)

POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")


# ─────────────────────────────────────────────────────────────────────────────
# SidePosition: one side (YES or NO) of a market
# ─────────────────────────────────────────────────────────────────────────────

class SidePosition:
    """Tracks shares, VWAP, and halt status for one side of a market.

    The critical invariant: USD exposure is ALWAYS computed as
        shares * clob_cost
    where clob_cost = avg_price for YES, (1 - avg_price) for NO.

    USD is never stored or accumulated. This makes it impossible for
    USD to drift from the actual position value.
    """

    __slots__ = ("side", "shares", "avg_price", "halted")

    def __init__(self, side: str, shares: float = 0.0,
                 avg_price: float = 0.0, halted: bool = False) -> None:
        self.side: str = side          # "yes" or "no" — immutable identity
        self.shares: float = shares    # Number of tokens held
        self.avg_price: float = avg_price  # VWAP in YES-equivalent terms
        self.halted: bool = halted     # True if position limit breached

    # ── Derived values (never stored) ────────────────────────────────────────

    @property
    def clob_cost(self) -> float:
        """Actual CLOB cost per share for this side.

        YES tokens cost avg_price on the CLOB.
        NO tokens cost (1 - avg_price) on the CLOB.
        """
        if self.avg_price <= 0:
            return 0.0
        return self.avg_price if self.side == "yes" else (1 - self.avg_price)

    @property
    def usd(self) -> float:
        """USD exposure — ALWAYS derived, never stored.

        This is the key design improvement over the old system where USD
        was accumulated per-fill and could drift from reality.
        """
        if self.shares < 0.01 or self.avg_price <= 0:
            return 0.0
        return self.shares * self.clob_cost

    @property
    def is_empty(self) -> bool:
        return self.shares < 1.0

    # ── State transitions ────────────────────────────────────────────────────

    def record_fill(self, new_shares: float, yes_equiv_price: float) -> float:
        """Update position after a BUY fill.

        Args:
            new_shares: Number of shares bought.
            yes_equiv_price: Fill price in YES-equivalent terms.
                For YES buys: this is the bid price.
                For NO buys: this is the YES ask price (NOT the NO CLOB price).

        Returns:
            The USD cost of this fill (for logging).
        """
        old_shares = self.shares
        old_avg = self.avg_price
        self.shares = old_shares + new_shares

        if self.shares > 0:
            self.avg_price = round(
                ((old_avg * old_shares) + (yes_equiv_price * new_shares))
                / self.shares,
                6,
            )
        else:
            self.avg_price = 0.0

        # Return actual CLOB cost for logging
        fill_clob_cost = (
            yes_equiv_price if self.side == "yes"
            else (1 - yes_equiv_price)
        )
        return new_shares * fill_clob_cost

    def record_unwind(self, sold_shares: float) -> None:
        """Update position after a SELL fill.

        Note: price is NOT needed because USD is derived from
        remaining shares * clob_cost. This eliminates the bug where
        wrong unwind price caused USD to go negative or stay inflated.

        Args:
            sold_shares: Number of shares sold.
        """
        self.shares = max(0.0, self.shares - sold_shares)
        if self.shares < 1.0:
            # Clean zero — prevent dust from blocking future operations
            self.shares = 0.0
            self.avg_price = 0.0
            self.halted = False

    def correct_to_exchange(self, actual_shares: float) -> None:
        """Set shares to match exchange reality.

        Called when exchange balance doesn't match our tracker.
        VWAP is preserved (best estimate of cost basis).
        Halt flag is preserved and will be re-checked by check_limits().

        Args:
            actual_shares: Actual token balance from exchange.
        """
        if actual_shares < 1.0:
            self.shares = 0.0
            # Preserve avg_price for P&L tracking on pending unwinds
            self.halted = False
        else:
            self.shares = actual_shares

    def check_limits(self) -> bool:
        """Update halt flag using hysteresis thresholds.

        Returns:
            True if halt state CHANGED (for logging/alerting).
        """
        usd = self.usd
        old_halted = self.halted

        if usd >= MAX_POSITION_USD and not self.halted:
            self.halted = True
        elif self.halted and usd <= RESUME_POSITION_USD:
            self.halted = False

        return self.halted != old_halted

    def reset(self) -> None:
        """Zero out all position data."""
        self.shares = 0.0
        self.avg_price = 0.0
        self.halted = False


# ─────────────────────────────────────────────────────────────────────────────
# PositionStore: manages all market positions
# ─────────────────────────────────────────────────────────────────────────────

class PositionStore:
    """Tracks positions across all active markets.

    Drop-in replacement for PositionTracker with these improvements:
    - USD is derived (shares * clob_cost), never accumulated
    - SidePosition objects know their side, so conversions are automatic
    - Exchange reconciliation is a first-class operation
    - Halt checks run on every state change, not just fills

    The public API is identical to PositionTracker for backward compatibility.
    """

    def __init__(self) -> None:
        # {condition_id: {"question": str, "yes": SidePosition, "no": SidePosition}}
        self._markets: dict[str, dict] = {}
        self._lock = threading.Lock()  # Thread safety for concurrent market cycles
        # B-5: pending position corrections queued by set_shares/reset_side and
        # flushed atomically with the next _save().
        self._pending_corrections: list[dict] = []
        self._load()

    # ── Internal accessors ───────────────────────────────────────────────────

    def _get_side(self, condition_id: str, side: str) -> SidePosition | None:
        """Get SidePosition for a market+side, or None if not tracked."""
        market = self._markets.get(condition_id)
        if market is None:
            return None
        return market[side.lower()]

    def _get_or_create_side(self, condition_id: str, side: str,
                            question: str = "") -> SidePosition:
        """Get or auto-register a market and return the SidePosition."""
        if condition_id not in self._markets:
            name = question or f"unknown-{condition_id[:12]}"
            log.warning(f"Auto-registering market on access: {name}")
            self.register_market(condition_id, name)
        return self._markets[condition_id][side.lower()]

    # ── Persistence (A3: SQLite primary, JSON fallback for migration) ──────

    def _load(self) -> None:
        """Load positions: try SQLite first, fall back to JSON for migration.

        Filters out test entries (condition_id containing 'test') so that
        test-suite data in the DB doesn't block the JSON migration path.
        """
        from database import get_db
        db = get_db()

        # Try SQLite first — filter out test entries
        data = db.load_all_positions()
        real_data = {
            cid: pos for cid, pos in data.items()
            if "test" not in cid.lower()
        } if data else {}

        if real_data:
            self._populate_from_dict(real_data)
            non_zero = sum(
                1 for m in self._markets.values()
                if not m["yes"].is_empty or not m["no"].is_empty
            )
            # Prune empty positions on load (cleanup stale entries)
            empty_cids = [
                cid for cid, m in self._markets.items()
                if (m["yes"].is_empty and m["no"].is_empty
                    and not m["yes"].halted and not m["no"].halted)
            ]
            for cid in empty_cids:
                del self._markets[cid]
            if empty_cids:
                log.info(f"Pruned {len(empty_cids)} empty positions on load")

            non_zero = len(self._markets)  # recount after pruning
            log.info(
                f"Loaded {non_zero} positions from SQLite "
                f"(with open exposure)"
            )
            # Also merge in any JSON positions not yet in SQLite
            self._merge_json_if_needed()
            return

        # Fall back to JSON (migration path)
        self._migrate_from_json()

    def _merge_json_if_needed(self) -> None:
        """Merge positions from JSON that aren't already in SQLite.

        Handles the case where SQLite has some markets but JSON has others
        that were never migrated (e.g., due to test data blocking migration).
        """
        if not os.path.exists(POSITIONS_FILE):
            return
        try:
            with open(POSITIONS_FILE, "r") as f:
                json_data = json.load(f)
            if not isinstance(json_data, dict):
                return

            merged_count = 0
            for cid, pos in json_data.items():
                if "test" in cid.lower():
                    continue
                if cid in self._markets:
                    continue  # Already have this market from SQLite
                # New market from JSON not in SQLite — add it
                question = pos.get("question", f"unknown-{cid[:12]}")
                yes = SidePosition(
                    side="yes",
                    shares=pos.get("yes_shares", 0.0),
                    avg_price=pos.get("yes_avg_price", 0.0),
                    halted=pos.get("yes_halted", False),
                )
                no = SidePosition(
                    side="no",
                    shares=pos.get("no_shares", 0.0),
                    avg_price=pos.get("no_avg_price", 0.0),
                    halted=pos.get("no_halted", False),
                )
                if not yes.is_empty or not no.is_empty:
                    self._markets[cid] = {
                        "question": question,
                        "yes": yes,
                        "no": no,
                    }
                    merged_count += 1
                    log.info(
                        f"Merged from JSON: {question[:40]} | "
                        f"YES={yes.shares:.1f} NO={no.shares:.1f}"
                    )

            if merged_count > 0:
                self._save()
                log.info(f"Merged {merged_count} position(s) from JSON → SQLite")

            # Rename JSON to .migrated now that everything is in SQLite
            try:
                migrated = POSITIONS_FILE + ".migrated"
                os.rename(POSITIONS_FILE, migrated)
                log.info(f"Renamed {POSITIONS_FILE} → {migrated}")
            except OSError as rename_err:
                log.warning(f"Could not rename positions.json: {rename_err}")

        except Exception as e:
            log.warning(f"Could not merge positions from JSON: {e}")

    def _migrate_from_json(self) -> None:
        """Full migration from JSON when SQLite has no real data."""
        if not os.path.exists(POSITIONS_FILE):
            return
        try:
            with open(POSITIONS_FILE, "r") as f:
                json_data = json.load(f)
            if not isinstance(json_data, dict):
                return

            # Filter out test entries from JSON too
            clean_data = {
                cid: pos for cid, pos in json_data.items()
                if "test" not in cid.lower()
            }
            self._populate_from_dict(clean_data)
            non_zero = sum(
                1 for m in self._markets.values()
                if not m["yes"].is_empty or not m["no"].is_empty
            )
            log.info(
                f"Migrated {len(self._markets)} positions from JSON to SQLite "
                f"({non_zero} with open exposure)"
            )
            # Save to SQLite immediately
            self._save()
            # Rename old JSON so it doesn't get loaded again
            try:
                migrated = POSITIONS_FILE + ".migrated"
                os.rename(POSITIONS_FILE, migrated)
                log.info(f"Renamed {POSITIONS_FILE} → {migrated}")
            except OSError as rename_err:
                log.warning(f"Could not rename positions.json: {rename_err}")

        except Exception as e:
            log.warning(f"Could not load positions from {POSITIONS_FILE}: {e}")

    def _populate_from_dict(self, data: dict) -> None:
        """Populate _markets from a flat position dict (JSON or SQLite format)."""
        for cid, pos in data.items():
            question = pos.get("question", f"unknown-{cid[:12]}")
            yes = SidePosition(
                side="yes",
                shares=pos.get("yes_shares", 0.0),
                avg_price=pos.get("yes_avg_price", 0.0),
                halted=pos.get("yes_halted", False),
            )
            no = SidePosition(
                side="no",
                shares=pos.get("no_shares", 0.0),
                avg_price=pos.get("no_avg_price", 0.0),
                halted=pos.get("no_halted", False),
            )
            self._markets[cid] = {
                "question": question,
                "yes": yes,
                "no": no,
            }

    def _save(self) -> None:
        """Persist current positions to SQLite (A3).

        Thread-safe: called within self._lock from all mutation methods.
        B-5: corrections queued by set_shares/reset_side are flushed atomically
        with the positions write, but only cleared after a successful commit.
        """
        from database import get_db
        try:
            pruned = {}
            for cid, market in self._markets.items():
                yes: SidePosition = market["yes"]
                no: SidePosition = market["no"]

                # Prune truly empty entries
                if (yes.is_empty and no.is_empty
                        and not yes.halted and not no.halted):
                    continue

                pruned[cid] = {
                    "question": market["question"],
                    "yes_shares": yes.shares,
                    "no_shares": no.shares,
                    "yes_avg_price": yes.avg_price,
                    "no_avg_price": no.avg_price,
                    "yes_halted": yes.halted,
                    "no_halted": no.halted,
                }

            ok = get_db().save_all_positions(pruned, self._pending_corrections)
            if ok:
                self._pending_corrections.clear()
        except Exception as e:
            log.error(f"Could not save positions to SQLite: {e}")

    # ── Public API (identical to PositionTracker) ────────────────────────────

    def register_market(self, condition_id: str, question: str) -> None:
        """Start tracking a new market."""
        if condition_id not in self._markets:
            self._markets[condition_id] = {
                "question": question,
                "yes": SidePosition("yes"),
                "no": SidePosition("no"),
            }
            log.debug(f"Registered market: {question[:50]}")
            self._save()

    def remove_market(self, condition_id: str) -> None:
        """Stop tracking a market."""
        if condition_id in self._markets:
            del self._markets[condition_id]
            self._save()

    def record_fill(
        self, condition_id: str, side: str, shares: float, price: float,
        question: str = "",
    ) -> None:
        """Record a BUY fill and check position limits.

        Thread-safe: acquires self._lock for the duration.

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".
            shares: Number of shares filled.
            price: Fill price in YES-equivalent terms.
            question: Market name (for auto-registration).
        """
        with self._lock:
            sp = self._get_or_create_side(condition_id, side, question)
            filled_usd = sp.record_fill(shares, price)

            question_str = self._markets[condition_id]["question"]
            yes_usd = self._markets[condition_id]["yes"].usd
            no_usd = self._markets[condition_id]["no"].usd
            log_position_update(question_str, yes_usd, no_usd)

            # Check limits and alert if halt state changed
            if sp.check_limits():
                if sp.halted:
                    alert_position_limit(question_str, side.upper(), sp.usd)
                else:
                    log.info(
                        f"QUOTING RESUMED | {question_str[:40]} | "
                        f"{side.upper()} back to ${sp.usd:.2f}"
                    )

            self._save()

    def record_unwind(
        self, condition_id: str, side: str, shares: float,
        price: float = 0.0,
    ) -> None:
        """Record a position reduction (SELL fill or merge).

        Thread-safe: acquires self._lock for the duration.

        Args:
            condition_id: Unique market identifier.
            side: "yes" or "no".
            shares: Number of shares sold.
            price: IGNORED — kept for backward compatibility.
                   USD is derived from remaining shares, not accumulated.
        """
        with self._lock:
            sp = self._get_side(condition_id, side)
            if sp is None:
                return

            sp.record_unwind(shares)

            question = self._markets[condition_id]["question"]
            log.info(
                f"Position unwound | {question[:40]} | "
                f"{side.upper()} reduced by {shares:.2f} shares "
                f"to {sp.shares:.2f} shares (${sp.usd:.2f})"
            )

            # Check if we can resume quoting
            if sp.check_limits():
                if not sp.halted:
                    log.info(
                        f"QUOTING RESUMED | {question[:40]} | "
                        f"{side.upper()} back to ${sp.usd:.2f}"
                    )

            self._save()

    def can_quote(self, condition_id: str, side: str) -> bool:
        """Check whether we are allowed to place new BUY orders on a side."""
        sp = self._get_side(condition_id, side)
        if sp is None:
            return True
        return not sp.halted

    def get_position(self, condition_id: str, side: str) -> float:
        """Get current USD exposure for one side (derived, not stored)."""
        sp = self._get_side(condition_id, side)
        if sp is None:
            return 0.0
        return sp.usd

    def get_shares(self, condition_id: str, side: str) -> float:
        """Get current share count for one side."""
        sp = self._get_side(condition_id, side)
        if sp is None:
            return 0.0
        return sp.shares

    def get_avg_price(self, condition_id: str, side: str) -> float:
        """Get volume-weighted average price for one side (YES-equivalent)."""
        sp = self._get_side(condition_id, side)
        if sp is None:
            return 0.0
        return sp.avg_price

    def get_all_positions(self) -> dict[str, dict]:
        """Return positions in the old flat-dict format for backward compat.

        This is used by bot.py for alerts, _remove_market checks, and
        _verify_positions_on_startup. Returns the same structure as the
        old PositionTracker.positions dict.
        """
        result = {}
        for cid, market in self._markets.items():
            yes: SidePosition = market["yes"]
            no: SidePosition = market["no"]
            result[cid] = {
                "yes": round(yes.usd, 2),
                "no": round(no.usd, 2),
                "yes_shares": yes.shares,
                "no_shares": no.shares,
                "yes_avg_price": yes.avg_price,
                "no_avg_price": no.avg_price,
                "yes_halted": yes.halted,
                "no_halted": no.halted,
                "question": market["question"],
            }
        return result

    def is_halted(self, condition_id: str, side: str) -> bool:
        """Check if a specific side is halted due to position limit."""
        sp = self._get_side(condition_id, side)
        if sp is None:
            return False
        return sp.halted

    def recalculate_usd(self) -> None:
        """Re-check all position limits on startup.

        In the old system this also fixed wrong USD values. In the new
        system USD is always derived, so this only needs to re-check
        halt flags (which may be stale from a previous session's
        positions.json).
        """
        changed = False
        for cid, market in self._markets.items():
            for side_key in ("yes", "no"):
                sp: SidePosition = market[side_key]
                if sp.is_empty:
                    continue
                if sp.check_limits():
                    changed = True
                    if sp.halted:
                        alert_position_limit(
                            market["question"], side_key.upper(), sp.usd
                        )
                    else:
                        log.info(
                            f"QUOTING RESUMED | {market['question'][:40]} | "
                            f"{side_key.upper()} back to ${sp.usd:.2f}"
                        )
        if changed:
            self._save()

    def reset_market(self, condition_id: str) -> None:
        """Reset all position data for a market."""
        market = self._markets.get(condition_id)
        if market is None:
            return
        market["yes"].reset()
        market["no"].reset()
        log.info(f"Position reset for: {condition_id}")
        self._save()

    def reset_side(self, condition_id: str, side: str) -> None:
        """Reset position data for one side of a market. Thread-safe."""
        with self._lock:
            sp = self._get_side(condition_id, side)
            if sp is None:
                return
            old_shares = sp.shares
            old_avg_price = sp.avg_price
            # B-5: only record a correction if there was something to reset.
            if old_shares >= 1.0 or old_avg_price > 0:
                self._pending_corrections.append({
                    "ts": time.time(),
                    "condition_id": condition_id,
                    "side": side.lower(),
                    "old_shares": old_shares,
                    "new_shares": 0.0,
                    "old_avg_price": old_avg_price,
                    "new_avg_price": 0.0,
                    "reason": "reset_side",
                })
            question = self._markets[condition_id]["question"]
            sp.reset()
            log.info(f"Position side reset | {question[:40]} | {side.upper()} zeroed")
            self._save()

    def set_shares(self, condition_id: str, side: str, shares: float,
                   avg_price: "float | None" = None) -> None:
        """Set share count to match exchange reality.

        FX-066 Tier 2: optional ``avg_price`` sets the cost basis when
        registering a position recovered from on-chain balance. Without it an
        orphan is registered with ``avg_price=0`` → ``get_avg_price=0`` →
        ``vwap_cost=0`` at dump time (loss recorded as profit) AND
        ``get_position()=0`` (invisible to notional guardrails). Pass the
        fills-reconstructed VWAP (``db.fills_vwap``) so every downstream
        consumer sees a real cost basis. ``None`` (default) preserves the
        existing ``avg_price`` — used by share-only corrections on
        already-tracked positions, which must not clobber a live VWAP.

        Thread-safe: acquires self._lock for the duration.
        """
        with self._lock:
            sp = self._get_side(condition_id, side)
            if sp is None:
                return
            old_shares = sp.shares
            old_avg_price = sp.avg_price
            sp.correct_to_exchange(shares)
            cost_note = ""
            if avg_price is not None and avg_price > 0:
                sp.avg_price = round(float(avg_price), 6)
                cost_note = f" (cost basis set avg_price={sp.avg_price:.4f})"
            # B-5: record a correction if shares or cost basis changed.
            new_shares = sp.shares
            new_avg_price = sp.avg_price
            if (abs(old_shares - new_shares) >= 1.0
                    or abs(old_avg_price - new_avg_price) > 0):
                self._pending_corrections.append({
                    "ts": time.time(),
                    "condition_id": condition_id,
                    "side": side.lower(),
                    "old_shares": old_shares,
                    "new_shares": new_shares,
                    "old_avg_price": old_avg_price,
                    "new_avg_price": new_avg_price,
                    "reason": "set_shares",
                })
            question = self._markets[condition_id]["question"]
            log.info(
                f"Position corrected | {question[:40]} | {side.upper()} "
                f"{old_shares:.2f} -> {shares:.2f} shares{cost_note}"
            )
            self._save()

    def print_summary(self) -> None:
        """Log a summary of all tracked positions."""
        if not self._markets:
            log.info("No positions currently tracked.")
            return
        log.info("-- Position Summary --")
        for cid, market in self._markets.items():
            yes: SidePosition = market["yes"]
            no: SidePosition = market["no"]
            yes_h = " [HALTED]" if yes.halted else ""
            no_h = " [HALTED]" if no.halted else ""
            log.info(
                f"  {market['question'][:45]} | "
                f"YES=${yes.usd:.2f} ({yes.shares:.1f} shares){yes_h} | "
                f"NO=${no.usd:.2f} ({no.shares:.1f} shares){no_h}"
            )
        log.info("-" * 50)

    # ── Backward compatibility ───────────────────────────────────────────────

    @property
    def positions(self) -> dict[str, dict]:
        """Backward-compat property for code that accesses .positions directly.

        Returns a live-updating dict-of-dicts view. Changes to the returned
        dicts do NOT propagate back (this is intentional — all mutations
        should go through the public API).
        """
        return self.get_all_positions()
