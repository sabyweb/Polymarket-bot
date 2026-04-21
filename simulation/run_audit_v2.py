"""simulation/run_audit_v2.py — Profit-Max Enforcement audit entry point.

Stricter than run_audit. Runs all 5 scenarios across multiple seeds (default
1, 42, 1337), applies v2 criteria (report_v2.build_report_v2), and reports
PASS only if EVERY seed independently passes ALL criteria.

    python -m simulation.run_audit_v2                  # defaults: 200 cycles, 3 seeds
    python -m simulation.run_audit_v2 --cycles 100
    python -m simulation.run_audit_v2 --seeds 7 99
    python -m simulation.run_audit_v2 --json audit_v2.json

Exit code: 0 on PASS, 1 on FAIL, 2 on usage error.
"""

from __future__ import annotations
import argparse
import json
import logging
import sys

from .engine import SimulationEngine
from .market_env import SCENARIOS
from .report_v2 import build_report_v2, print_report_v2


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
        description="Polymarket bot v2 audit: Profit-Max Enforcement",
    )
    parser.add_argument(
        "--cycles", type=int, default=200,
        help="cycles per scenario per seed (default 200)",
    )
    parser.add_argument(
        "--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS),
        help=f"list of seeds to run (default {list(DEFAULT_SEEDS)})",
    )
    parser.add_argument(
        "--scenarios", nargs="+", default=list(SCENARIOS),
        help=f"subset of scenarios (default: all 5 = {list(SCENARIOS)})",
    )
    parser.add_argument(
        "--json", type=str, default=None,
        help="write the JSON report to this path",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="enable INFO logging from production modules",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    for s in args.scenarios:
        if s not in SCENARIOS:
            print(
                f"unknown scenario {s!r}; valid: {SCENARIOS}",
                file=sys.stderr,
            )
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

    report = build_report_v2(results_by_seed)
    print_report_v2(report)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"report written to {args.json}")

    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
