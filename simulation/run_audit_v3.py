"""simulation/run_audit_v3.py — Overcommitment-aware V3 audit entry point.

Runs all 5 scenarios across default seeds (1, 42, 1337), computes V3
per-cycle expected-capital metrics, evaluates V3 invariants + success
criteria, and reports PASS only if EVERY seed independently passes.

    python -m simulation.run_audit_v3                   # 200 cycles, 3 seeds
    python -m simulation.run_audit_v3 --cycles 400
    python -m simulation.run_audit_v3 --seeds 7 99
    python -m simulation.run_audit_v3 --json audit_v3.json

Exit code: 0 on PASS, 1 on FAIL, 2 on usage error.
"""

from __future__ import annotations
import argparse
import json
import logging
import sys

from .engine import SimulationEngine
from .market_env import SCENARIOS
from .report_v3 import build_report_v3, print_report_v3


DEFAULT_SEEDS = (1, 42, 1337)


def _setup_logging(verbose: bool) -> None:
    level = logging.WARNING if not verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Polymarket bot V3 audit: overcommitment-aware",
    )
    parser.add_argument(
        "--cycles", type=int, default=200,
        help="cycles per scenario per seed (default 200)",
    )
    parser.add_argument(
        "--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS),
        help=f"list of seeds (default {list(DEFAULT_SEEDS)})",
    )
    parser.add_argument(
        "--scenarios", nargs="+", default=list(SCENARIOS),
        help=f"subset of scenarios (default all 5 = {list(SCENARIOS)})",
    )
    parser.add_argument(
        "--json", type=str, default=None,
        help="write the full JSON report to this path",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="enable INFO-level logging",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    for s in args.scenarios:
        if s not in SCENARIOS:
            print(f"unknown scenario {s!r}; valid: {SCENARIOS}", file=sys.stderr)
            return 2

    results_by_seed: dict = {}
    for seed in args.seeds:
        engine = SimulationEngine(seed=seed)
        seed_results = []
        for scenario in args.scenarios:
            print(
                f"[seed={seed}] running scenario={scenario} "
                f"cycles={args.cycles}..."
            )
            r = engine.run(scenario=scenario, cycles=args.cycles)
            seed_results.append(r)
        results_by_seed[seed] = seed_results

    report = build_report_v3(results_by_seed)
    print_report_v3(report)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"report written to {args.json}")

    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
