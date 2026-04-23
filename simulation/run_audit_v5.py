"""simulation/run_audit_v5.py — CLI entrypoint for Audit V5.

V5 evaluates the continuous allocator against invariants aligned to its
design (expected capital utilisation + allocation coverage), keeping V7
oscillation stability unchanged. V4 files are untouched; this module
reuses the V4 scenarios, engine wrapper, and cycle tracker wholesale.

Usage:

    python3 -m simulation.run_audit_v5 \\
        --cycles 500 \\
        --seeds 1 42 1337 \\
        --log-detailed \\
        --trace-invariants \\
        --dump-timeseries

Produces:

    stdout                             summary table + (optional) per-cycle
                                       detail + failure trace.
    <out-dir>/audit_v5_report.json     machine-readable verdict.
    <out-dir>/<scenario>/seed_<N>/     per-run CSVs (capital_scale,
                                       flip_rate, expected_util,
                                       coverage_ratio) + full_snapshots.jsonl.

Exit code: 0 when every scenario × seed passes all three invariants;
1 otherwise.
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import sys

from profit.learning import LearningController

# Sim-only calibrator wrapper — fixes the bootstrap p_fill=0 collapse
# that would otherwise drive expected_capital ≈ 0 before FillModel
# trains. Production calibration code is unaffected.
from .bootstrap_calibrator import make_sim_calibrator

# Reuse the V4 engine scaffolding verbatim — DO NOT duplicate the loop.
from .engine import (
    SimulationEngine, _deterministic_environment, _SimClock,
)
from .runner import execute_cycle, TOTAL_CAPITAL
from .audit_v4_scenarios import AuditV4Environment, AUDIT_V4_SCENARIOS
from .audit_v4_metrics import V4Tracker, V4CycleSnapshot
from .audit_v5_invariants import (
    evaluate_all_v5, validate_v5_fields, V5FieldMissingError,
)
from .audit_v5_report import (
    V5SeedResult, build_scenario_verdicts_v5, build_json_report_v5,
    dump_timeseries_v5, emit_failure_diagnostics_v5, emit_summary_table_v5,
)


log = logging.getLogger("audit_v5")


# ═══════════════════════════════════════════════════════════════
# Per-(scenario, seed) run
# ═══════════════════════════════════════════════════════════════

def run_one(
    scenario: str,
    seed: int,
    cycles: int,
    log_detailed: bool = False,
) -> V5SeedResult:
    """Run one (scenario, seed) combination and return a V5SeedResult.

    Uses V4's scenario definitions + V4Tracker (the per-cycle data the
    tracker captures is a superset of what V5 invariants read). After
    each cycle we validate the continuous allocator's per-market
    stamps (spec §3.2) so a missing field surfaces as a deterministic
    V5FieldMissingError instead of silent NaN propagation downstream.
    """
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
) -> V5SeedResult:
    import random
    # Same RNG layering V4 uses so (scenario, seed) is bit-identical
    # across V4 and V5 runs — only the invariant evaluators differ.
    master = random.Random(seed)
    market_seed = master.randint(0, 2**31 - 1)
    fill_seed = master.randint(0, 2**31 - 1)
    market_rng_seed = master.randint(0, 2**31 - 1)

    env = AuditV4Environment(
        scenario=scenario, seed=market_seed, total_cycles=cycles,
    )
    fill_rng = random.Random(fill_seed)
    market_rng = random.Random(market_rng_seed)

    calibrator = make_sim_calibrator(db_path=db_path)
    learn_ctrl = LearningController(db_path=db_path, alloc_path=alloc_path)

    tracker = V4Tracker(db_path=db_path)
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

        # Spec §3.2: raise on missing per-market fields. Unlike V4 which
        # required Patch 7/11/13 stamps, V5 requires only the fields the
        # continuous allocator emits on every deploy row.
        validate_v5_fields(
            outcome.allocations,
            cycle=cycle,
            scenario=scenario,
            seed=seed,
            total_capital=outcome.total_capital,
        )

        snap = tracker.snapshot(cycle, outcome)
        snapshots.append(snap)

        if log_detailed:
            # Derived V5 metrics for the detailed log — no mutation.
            from .audit_v5_invariants import (
                compute_expected_util, compute_coverage_ratio,
            )
            eu = compute_expected_util(snap)
            cov = compute_coverage_ratio(snap)
            log.info(
                "[V5 %s seed=%d cy=%04d] mode=%s deploy=%d "
                "expected_util=%s coverage=%s cap=%.3f dir=%d lock=%d "
                "flip_rate=%.1f",
                scenario, seed, cycle, snap.mode,
                snap.number_of_deployed_markets,
                f"{eu:.4f}" if eu is not None else "—",
                f"{cov:.3f}" if cov is not None else "—",
                snap.capital_scale, snap.last_direction,
                snap.direction_lock, snap.rolling_flip_rate_100,
            )

    invariants = evaluate_all_v5(snapshots)
    return V5SeedResult(
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
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if not detailed:
        for name in ("profit.allocator", "profit.learning",
                     "oversight.data_collector", "calibration.manager"):
            logging.getLogger(name).setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m simulation.run_audit_v5",
        description=(
            "Audit V5: system-level simulation verifying INV3_new / "
            "INV5_new / INV7 against the continuous allocator."
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
            f"(default all = {list(AUDIT_V4_SCENARIOS)})"
        ),
    )
    parser.add_argument(
        "--log-detailed", action="store_true",
        help="enable per-cycle INFO logging",
    )
    parser.add_argument(
        "--trace-invariants", action="store_true",
        help="emit structured failure diagnostics to stdout "
             "(force-enabled on any FAIL verdict)",
    )
    parser.add_argument(
        "--dump-timeseries", action="store_true",
        help="write per-run CSV + JSONL timeseries under --out-dir",
    )
    parser.add_argument(
        "--out-dir", default="audit_v5_out",
        help="output directory for JSON report + timeseries dumps",
    )
    parser.add_argument(
        "--json", default=None,
        help="path for the machine-readable JSON report "
             "(default: <out-dir>/audit_v5_report.json)",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_detailed)

    for s in args.scenarios:
        if s not in AUDIT_V4_SCENARIOS:
            print(
                f"unknown scenario {s!r}; valid: {AUDIT_V4_SCENARIOS}",
                file=sys.stderr,
            )
            return 2

    # Run every (scenario, seed). Preserve argument order.
    results: list[V5SeedResult] = []
    for scenario in args.scenarios:
        for seed in args.seeds:
            print(
                f"[V5] running scenario={scenario} seed={seed} "
                f"cycles={args.cycles} ...",
                flush=True,
            )
            try:
                result = run_one(
                    scenario=scenario,
                    seed=seed,
                    cycles=args.cycles,
                    log_detailed=args.log_detailed,
                )
            except V5FieldMissingError as e:
                # Strict contract — bubble up with a clean summary.
                print(f"    → FIELD-MISSING: {e}", flush=True)
                print(
                    "\n=== AUDIT V5 OVERALL: FAIL "
                    "(allocator contract violation) ===",
                    flush=True,
                )
                return 1
            inv = result.invariants
            print(
                f"    → INV3_new={'PASS' if inv['INV3_new'].passed else 'FAIL'} "
                f"INV5_new={'PASS' if inv['INV5_new'].passed else 'FAIL'} "
                f"INV7={'PASS' if inv['INV7'].passed else 'FAIL'}",
                flush=True,
            )
            results.append(result)

    verdicts = build_scenario_verdicts_v5(results)
    print()
    print(emit_summary_table_v5(verdicts))
    print()

    any_fail = any(v.verdict == "FAIL" for v in verdicts)
    if args.trace_invariants or any_fail:
        print(emit_failure_diagnostics_v5(verdicts))

    os.makedirs(args.out_dir, exist_ok=True)
    json_path = args.json or os.path.join(
        args.out_dir, "audit_v5_report.json",
    )
    with open(json_path, "w") as f:
        json.dump(
            build_json_report_v5(verdicts, args.cycles),
            f, indent=2, default=str,
        )
    print(f"JSON report: {json_path}")

    if args.dump_timeseries:
        written = dump_timeseries_v5(args.out_dir, results)
        print(f"Wrote {len(written)} timeseries files under {args.out_dir}/")

    overall = "PASS" if not any_fail else "FAIL"
    print(f"\n=== AUDIT V5 OVERALL: {overall} ===")
    return 0 if not any_fail else 1


if __name__ == "__main__":
    sys.exit(main())
