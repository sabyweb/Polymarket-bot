"""simulation/run_audit.py — CLI entry point.

    python -m simulation.run_audit                      # default 200 cycles
    python -m simulation.run_audit --cycles 50          # smoke test
    python -m simulation.run_audit --seed 7 --cycles 50
    python -m simulation.run_audit --json out.json      # write report

Exit code:
    0 on STATUS=PASS
    1 on STATUS=FAIL
"""

from __future__ import annotations
import argparse
import json
import logging
import sys

from .engine import SimulationEngine
from .market_env import SCENARIOS
from .report import build_report, print_report


def _setup_logging(verbose: bool) -> None:
    level = logging.WARNING if not verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Polymarket bot end-to-end audit harness",
    )
    parser.add_argument(
        "--cycles", type=int, default=200,
        help="cycles per scenario (default 200)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="master seed for determinism (default 42)",
    )
    parser.add_argument(
        "--json", type=str, default=None,
        help="write the full JSON report to this path",
    )
    parser.add_argument(
        "--scenarios", nargs="+", default=list(SCENARIOS),
        help="subset of scenarios to run (default: all)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="enable INFO-level logging from production modules",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    engine = SimulationEngine(seed=args.seed)
    results = []
    for scenario in args.scenarios:
        if scenario not in SCENARIOS:
            print(
                f"unknown scenario {scenario!r}; valid: {SCENARIOS}",
                file=sys.stderr,
            )
            return 2
        print(f"running scenario={scenario} cycles={args.cycles}...")
        result = engine.run(scenario=scenario, cycles=args.cycles)
        results.append(result)

    report = build_report(results)
    print_report(report)

    if args.json:
        # Strip non-JSON-serializable bits before dumping.
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"report written to {args.json}")

    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
