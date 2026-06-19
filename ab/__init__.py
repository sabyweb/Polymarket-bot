"""ab/ — offline A/B + per-market NET analysis harness (read-only).

Operates ONLY on a DB snapshot (snapshots/<date>/), never the live WAL.
Offline is a FILTER, not proof; the live soak is the proof (ground_rules.md).
"""
