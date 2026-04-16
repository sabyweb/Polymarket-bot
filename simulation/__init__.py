"""simulation/ — deterministic adversarial audit harness.

Wraps (does NOT modify) production modules to validate end-to-end
behavior, learning correctness, capital efficiency, and invariant
enforcement under five adversarial market scenarios.

Entry point:
    python -m simulation.run_audit

Modules:
    market_env  — synthetic market signal generator (5 scenarios)
    metrics     — per-cycle metric tracker + trend analysis
    invariants  — hard invariant checker (clamps, capital, EV gates)
    runner      — single-cycle execution helper
    engine      — top-level SimulationEngine
    report      — PASS/FAIL audit report builder
"""
