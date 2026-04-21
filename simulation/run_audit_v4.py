"""simulation/run_audit_v4.py — CLI entrypoint for Audit V4.

Usage:

    python3 -m simulation.run_audit_v4 \\
        --cycles 500 \\
        --seeds 1 42 1337 \\
        --log-detailed \\
        --trace-invariants \\
        --dump-timeseries

Produces:
    stdout  — summary table + (optional) per-cycle detail + failure trace
    <out-dir>/audit_v4_report.json     — machine-readable verdict
    <out-dir>/<scenario>/seed_<N>/     — per-run CSVs (timeseries) +
                                          full_snapshots.jsonl

Exit code: 0 when every scenario × seed passes all three invariants;
1 otherwise.

The audit does NOT stub allocator or learning-loop logic. It drives the
real production modules through a deterministic wrapper identical to
the V3 audit (same `_deterministic_environment` context, same
`_SimClock`, same DB schema) — V4 only differs in:
  - scenario set (6 instead of 5, D extended to 3 phases, E + F new)
  - per-cycle metric capture (Patch 11/13 stamps in addition to V3 set)
  - invariant definitions (V4 thresholds per spec section 5)
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import sys

from calibration.manager import CalibrationManager
from profit.learning import LearningController

# Reuse engine internals — DO NOT duplicate production logic.
from .engine import (
    SimulationEngine, _deterministic_environment, _SimClock,
)
from .runner import execute_cycle, TOTAL_CAPITAL
from .audit_v4_scenarios import AuditV4Environment, AUDIT_V4_SCENARIOS
from .audit_v4_metrics import V4Tracker, V4CycleSnapshot
from .audit_v4_invariants import evaluate_all
from .audit_v4_report import (
    V4SeedResult, build_scenario_verdicts, build_json_report,
    dump_timeseries, emit_failure_diagnostics, emit_summary_table,
)


log = logging.getLogger("audit_v4")


# ═══════════════════════════════════════════════════════════════
# Per-(scenario, seed) run
# ═══════════════════════════════════════════════════════════════

def run_one(
    scenario: str,
    seed: int,
    cycles: int,
    log_detailed: bool = False,
) -> V4SeedResult:
    """Run one (scenario, seed) combination and return V4SeedResult.

    Mirrors SimulationEngine._run_inner's structure but swaps in the
    V4 environment and V4 metrics tracker. Invariant evaluation runs
    after the loop.
    """
    # The engine owns DB creation + clock patching. We instantiate it
    # only to reuse its private _create_db helper. The actual run loop
    # lives here so we can inject V4 metrics capture.
    engine = SimulationEngine(seed=seed)
    db_path = engine._create_db()
    alloc_path = engine._create_alloc_path()

    try:
        with _deterministic_environment(seed) as sim_clock:
            return _run_loop(
                scenario=scenario,
                seed=seed,
                cycles=cycles,
                db_path=db_path,
                alloc_path=alloc_path,
                sim_clock=sim_clock,
                log_detailed=log_detailed,
            )
    finally:
        for p in (db_path, alloc_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _run_loop(
    scenario: str,
    seed: int,
    cycles: int,
    db_path: str,
    alloc_path: str,
    sim_clock: _SimClock,
    log_detailed: bool,
) -> V4SeedResult:
    import random
    # Same RNG layering as SimulationEngine._run_inner so the V4 audit
    # consumes the same seed → sequence mapping as V3 does (for the
    # wrapped scenarios A / B / C).
    master = random.Random(seed)
    market_seed = master.randint(0, 2**31 - 1)
    fill_seed = master.randint(0, 2**31 - 1)
    market_rng_seed = master.randint(0, 2**31 - 1)

    env = AuditV4Environment(
        scenario=scenario, seed=market_seed, total_cycles=cycles,
    )
    fill_rng = random.Random(fill_seed)
    market_rng = random.Random(market_rng_seed)

    calibrator = CalibrationManager(db_path=db_path)
    learn_ctrl = LearningController(db_path=db_path, alloc_path=alloc_path)

    tracker = V4Tracker()
    snapshots: list[V4CycleSnapshot] = []

    for cycle in range(cycles):
        sim_clock.advance_to_cycle(cycle)
        signals = env.signals_for(cycle)
        outcome = execute_cycle(
            cycle=cycle,
            db_path=db_path,
            alloc_path=alloc_path,
            calibrator=calibrator,
            learn_ctrl=learn_ctrl,
            signals=signals,
            market_rng=market_rng,
            fill_rng=fill_rng,
            total_capital=TOTAL_CAPITAL,
        )
        snap = tracker.snapshot(cycle, outcome)
        snapshots.append(snap)
        if log_detailed:
            log.info(
                "[V4 %s seed=%d cy=%04d] mode=%s deploy_ratio=%.3f "
                "total=$%.0f target=%s oc=%s cap=%.3f dir=%d lock=%d "
                "flip_rate=%.1f forced=%.1f%%",
                scenario, seed, cycle, snap.mode, snap.deploy_ratio,
                snap.total_notional,
                f"${snap.target_notional:.0f}" if snap.target_notional is not None else "—",
                f"{snap.overcommit_factor:.2f}" if snap.overcommit_factor is not None else "—",
                snap.capital_scale, snap.last_direction,
                snap.direction_lock, snap.rolling_flip_rate_100,
                snap.percent_forced_target_alloc * 100.0,
            )

    invariants = evaluate_all(snapshots)
    return V4SeedResult(
        scenario=scenario,
        seed=seed,
        cycles=cycles,
        snapshots=snapshots,
        invariants=invariants,
    )


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def _setup_logging(detailed: bool) -> None:
    level = logging.INFO if detailed else logging.WARNING
    # Route ours to INFO, suppress production modules (they'd flood output).
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Production modules log a LOT. Quiet them below WARNING by default,
    # but let them through at detail level so --log-detailed shows them.
    if not detailed:
        for name in ("profit.allocator", "profit.learning",
                     "oversight.data_collector", "calibration.manager"):
            logging.getLogger(name).setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m simulation.run_audit_v4",
        description=(
            "Audit V4: system-level simulation verifying INV3 / INV5 / "
            "INV7 under six deterministic scenarios."
        ),
    )
    parser.add_argument(
        "--cycles", type=int, default=500,
        help="total simulation steps per run (default 500)",
    )
    parser.add_argument(
        "--seeds", nargs="+", type=int, default=[1, 42, 1337],
        help="list of deterministic seeds (default: 1 42 1337)",
    )
    parser.add_argument(
        "--scenarios", nargs="+", default=list(AUDIT_V4_SCENARIOS),
        help=(
            f"subset of scenarios to run "
            f"(default all 6 = {list(AUDIT_V4_SCENARIOS)})"
        ),
    )
    parser.add_argument(
        "--log-detailed", action="store_true",
        help="enable per-cycle INFO logging",
    )
    parser.add_argument(
        "--trace-invariants", action="store_true",
        help="emit structured failure diagnostics to stdout",
    )
    parser.add_argument(
        "--dump-timeseries", action="store_true",
        help="write per-run CSV + JSONL timeseries under --out-dir",
    )
    parser.add_argument(
        "--out-dir", default="audit_v4_out",
        help="output directory for JSON report + timeseries dumps",
    )
    parser.add_argument(
        "--json", default=None,
        help="path for the machine-readable JSON report "
             "(default: <out-dir>/audit_v4_report.json)",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_detailed)

    # Validate scenario list.
    for s in args.scenarios:
        if s not in AUDIT_V4_SCENARIOS:
            print(
                f"unknown scenario {s!r}; valid: {AUDIT_V4_SCENARIOS}",
                file=sys.stderr,
            )
            return 2

    # Run every (scenario, seed) combination. Preserve the argument
    # order so the summary table row order mirrors CLI intent.
    results: list[V4SeedResult] = []
    for scenario in args.scenarios:
        for seed in args.seeds:
            print(
                f"[V4] running scenario={scenario} seed={seed} "
                f"cycles={args.cycles} ...",
                flush=True,
            )
            result = run_one(
                scenario=scenario,
                seed=seed,
                cycles=args.cycles,
                log_detailed=args.log_detailed,
            )
            # Per-run headline for the operator.
            inv = result.invariants
            print(
                f"    → INV3={'PASS' if inv['INV3'].passed else 'FAIL'} "
                f"INV5={'PASS' if inv['INV5'].passed else 'FAIL'} "
                f"INV7={'PASS' if inv['INV7'].passed else 'FAIL'}",
                flush=True,
            )
            results.append(result)

    verdicts = build_scenario_verdicts(results)
    print()
    print(emit_summary_table(verdicts))
    print()

    # Failure diagnostics — only when requested OR when any scenario fails.
    any_fail = any(v.verdict == "FAIL" for v in verdicts)
    if args.trace_invariants or any_fail:
        print(emit_failure_diagnostics(verdicts))

    # JSON report.
    os.makedirs(args.out_dir, exist_ok=True)
    json_path = args.json or os.path.join(args.out_dir, "audit_v4_report.json")
    with open(json_path, "w") as f:
        json.dump(
            build_json_report(verdicts, args.cycles),
            f, indent=2, default=str,
        )
    print(f"JSON report: {json_path}")

    # Timeseries dumps.
    if args.dump_timeseries:
        written = dump_timeseries(args.out_dir, results)
        print(f"Wrote {len(written)} timeseries files under {args.out_dir}/")

    overall = "PASS" if not any_fail else "FAIL"
    print(f"\n=== AUDIT V4 OVERALL: {overall} ===")
    return 0 if not any_fail else 1


if __name__ == "__main__":
    sys.exit(main())
