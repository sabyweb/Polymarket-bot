"""profit/refill.py — PATCH 7 fill-triggered refill helpers.

Pure functions called after a fill event to:

  1. Update remaining capital (`handle_fill_event`)
  2. Partition open orders into keep/cancel based on what the new capital
     can still support (`cancel_unfunded_orders`)
  3. Produce a refill plan (`plan_refill_after_fill`) summarising the post-
     fill state and signalling that a reallocation should follow.

These helpers are SIDE-EFFECT-FREE — no DB, no CLOB calls, no logging.
The execution layer (reward_farmer / order_lifecycle) is expected to
invoke the real cancel + place-orders plumbing based on the returned
plan. Keeping the logic in pure functions makes it trivially testable
and safe to import from both production and tests.

Contract:
  • `open_orders` items are dicts with at least `size` (float) and
    `priority` (number; higher = keep-first). An `order_id` key is
    recommended for the caller's cancel plumbing but not required here.
  • `remaining_capital` is the available balance AFTER the fill.
  • Sorting is stable: same-priority orders keep their input order.

Invariants enforced by these helpers:
  • Kept orders' size sum ≤ remaining_capital (never over-commit the
    post-fill balance in the KEEP set).
  • Every input order ends up in exactly one of (keep, cancel).
  • remaining_capital never goes negative by adding a kept order
    (caller's responsibility is to cancel the rest and free capital).
"""

from __future__ import annotations
from typing import Any


def detect_fill_event(
    prev_positions: dict, current_positions: dict,
) -> bool:
    """PATCH 11 — positional-diff fill detector.

    Returns True when any key in `current_positions` has a value strictly
    greater than its counterpart in `prev_positions` (missing keys default
    to 0). Used by the execution layer to notice that a resting order was
    filled between snapshots without relying on a streaming fill feed.

    Values are coerced to float so ints, floats, and share-count Decimals
    all compare cleanly. Order of keys is irrelevant; the first increase
    short-circuits.

    Pure helper — no DB, no CLOB calls, no logging. Wiring into
    `reward_farmer` / `order_lifecycle` is deliberately deferred to a
    separate patch (Part 2.4b) so the execution-layer change can be
    reviewed and tested in isolation.
    """
    for k, cur in current_positions.items():
        prev_val = prev_positions.get(k, 0)
        if float(cur) > float(prev_val):
            return True
    return False


def handle_fill_event(
    fill_cost: float, remaining_capital_pre: float,
) -> float:
    """Compute post-fill remaining capital.

    A fill consumes `fill_cost` from the balance. `remaining_capital_pre`
    is the pre-fill value; the returned value is clamped at zero — a
    negative result means the caller's accounting was out of sync and
    the execution layer should log/raise rather than proceed.
    """
    post = float(remaining_capital_pre) - float(fill_cost)
    return max(0.0, post)


def cancel_unfunded_orders(
    open_orders: list[dict], remaining_capital: float,
) -> tuple[list[dict], list[dict]]:
    """Partition `open_orders` into (keep, cancel).

    Greedy algorithm:
      1. Sort orders by priority descending (stable — preserves input
         order for ties).
      2. Walk the sorted list, accumulating sizes. Each order is kept
         IFF adding its size would NOT exceed `remaining_capital`.
         Otherwise it goes to cancel.

    Returns:
      (keep_orders, cancel_orders) — both lists are fresh (no mutation
      of input). `keep_orders` size sum ≤ remaining_capital.
    """
    if remaining_capital <= 0:
        return [], list(open_orders)

    # Sort by -priority (highest first). Python's sort is stable, so
    # orders with the same priority retain their input ordering.
    sorted_orders = sorted(
        open_orders, key=lambda o: -float(o.get("priority", 0)),
    )
    keep: list[dict] = []
    cancel: list[dict] = []
    running = 0.0
    for o in sorted_orders:
        size = float(o.get("size", 0) or 0.0)
        if running + size <= float(remaining_capital) + 1e-9:
            keep.append(o)
            running += size
        else:
            cancel.append(o)
    return keep, cancel


def plan_refill_after_fill(
    fill_cost: float,
    remaining_capital_pre: float,
    open_orders: list[dict],
) -> dict:
    """Compute a full refill plan for the execution layer.

    Returns a dict with:
      - remaining_capital  : post-fill balance (>= 0)
      - keep_orders        : orders that fit within the new capital
      - cancel_orders      : orders that must be cancelled
      - should_reallocate  : True — caller should request a fresh
                             allocate_portfolio() pass to refill slots
                             freed by the cancellations.

    The caller is responsible for translating `cancel_orders` into real
    CLOB cancellation calls and passing `keep_orders` through to the
    next reallocation.
    """
    remaining = handle_fill_event(fill_cost, remaining_capital_pre)
    keep, cancel = cancel_unfunded_orders(open_orders, remaining)
    return {
        "remaining_capital": remaining,
        "keep_orders": keep,
        "cancel_orders": cancel,
        "should_reallocate": True,
        "n_cancelled": len(cancel),
        "n_kept": len(keep),
    }
