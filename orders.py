"""Backward-compatibility shim — imports from order_manager.py.

All logic now lives in focused modules:
  - order_manager.py  — OrderManager class, BalanceGate, shared state & utilities
  - pricing.py        — PricingMixin (co-best pricing, inventory skew, zones)
  - placement.py      — PlacementMixin (BUY order placement, balance gating)
  - fills.py          — FillsMixin (fill detection, order adoption)
  - unwind.py         — UnwindMixin (SELL orders, decay, reconciliation, merges)
"""
from order_manager import OrderManager, BalanceGate, TrackedOrder, UnwindOrder

__all__ = ["OrderManager", "BalanceGate", "TrackedOrder", "UnwindOrder"]
