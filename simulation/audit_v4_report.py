"""simulation/audit_v4_report.py — V4 audit output generation.

Three output surfaces:

  A. Summary table  — one row per scenario with INV3/5/7 + verdict
                      (PASS if all seeds pass; FAIL if any seed fails)
  B. Failure diagnostics — per failed invariant: scenario, seeds,
                           failure window, root-cause hint
  C. Timeseries dumps — per (scenario, seed): capital_scale.csv,
                        deploy_ratio.csv, overcommit.csv, flip_rate.csv

All three are called from `run_audit_v4.main()`; this module contains
no I/O side-effects aside from file writes under a caller-supplied
output directory.
"""

from __future__ import annotations
import csv
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .audit_v4_metrics import V4CycleSnapshot
from .audit_v4_invariants import V4InvariantResult


@dataclass
class V4SeedResult:
    """One (scenario, seed) simulation run."""
    scenario: str
    seed: int
    cycles: int
    snapshots: list[V4CycleSnapshot]
    invariants: dict[str, V4InvariantResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.invariants.values())


@dataclass
class V4ScenarioVerdict:
    """Aggregate verdict for one scenario across all seeds."""
    scenario: str
    seeds: list[int]
    inv3_all_pass: bool
    inv5_all_pass: bool
    inv7_all_pass: bool
    per_seed: list[V4SeedResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return "PASS" if (
            self.inv3_all_pass and self.inv5_all_pass and self.inv7_all_pass
        ) else "FAIL"


def build_scenario_verdicts(
    results: list[V4SeedResult],
) -> list[V4ScenarioVerdict]:
    """Group results by scenario and compute an aggregate verdict.
    Preserves the first-seen scenario order so the output mirrors the
    CLI's scenario argument order."""
    buckets: dict[str, list[V4SeedResult]] = {}
    order: list[str] = []
    for r in results:
        if r.scenario not in buckets:
            buckets[r.scenario] = []
            order.append(r.scenario)
        buckets[r.scenario].append(r)

    out: list[V4ScenarioVerdict] = []
    for scenario in order:
        seed_results = buckets[scenario]
        seeds = [r.seed for r in seed_results]
        inv3 = all(r.invariants["INV3"].passed for r in seed_results)
        inv5 = all(r.invariants["INV5"].passed for r in seed_results)
        inv7 = all(r.invariants["INV7"].passed for r in seed_results)
        out.append(V4ScenarioVerdict(
            scenario=scenario,
            seeds=seeds,
            inv3_all_pass=inv3,
            inv5_all_pass=inv5,
            inv7_all_pass=inv7,
            per_seed=seed_results,
        ))
    return out


# ═══════════════════════════════════════════════════════════════
# A. Summary table
# ═══════════════════════════════════════════════════════════════

def emit_summary_table(verdicts: list[V4ScenarioVerdict]) -> str:
    """Return a plain-text Markdown table summarising results.

    Format exactly matches the spec:
        | Scenario | INV3 | INV5 | INV7 | Verdict |
    """
    lines = [
        "=== AUDIT V4 — SCENARIO SUMMARY ===",
        "",
        "| Scenario              | INV3 | INV5 | INV7 | Verdict |",
        "|-----------------------|------|------|------|---------|",
    ]
    for v in verdicts:
        lines.append(
            f"| {v.scenario:<21} | "
            f"{'PASS' if v.inv3_all_pass else 'FAIL'} | "
            f"{'PASS' if v.inv5_all_pass else 'FAIL'} | "
            f"{'PASS' if v.inv7_all_pass else 'FAIL'} | "
            f"{v.verdict:<7} |"
        )
    lines.append("")
    all_pass = all(v.verdict == "PASS" for v in verdicts)
    lines.append(f"Overall: {'PASS' if all_pass else 'FAIL'}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# B. Failure diagnostics
# ═══════════════════════════════════════════════════════════════

def emit_failure_diagnostics(verdicts: list[V4ScenarioVerdict]) -> str:
    """Per failed invariant, emit:
         [INVx FAIL]
         scenario=<name>
         seed=<n>
         cycles=<start>-<end>
         <metric key>=<value> ...
         reason=<root-cause hint>

    Multi-seed failures are reported once per seed so the log is
    greppable. When a scenario passes everywhere we emit nothing for it.
    """
    blocks: list[str] = []
    for v in verdicts:
        for seed_result in v.per_seed:
            for inv_name, inv_result in seed_result.invariants.items():
                if inv_result.passed:
                    continue
                failing_ranges = inv_result.failing_cycles or [(0, 0)]
                for start, end in failing_ranges:
                    lines = [f"[{inv_name} FAIL]"]
                    lines.append(f"scenario={v.scenario}")
                    lines.append(f"seed={seed_result.seed}")
                    lines.append(f"cycles={start}-{end}")
                    for k, val in inv_result.metric_values.items():
                        lines.append(f"{k}={val}")
                    if inv_result.reason:
                        lines.append(f"reason={inv_result.reason}")
                    blocks.append("\n".join(lines))
    if not blocks:
        return "No invariant failures.\n"
    return (
        "=== AUDIT V4 — FAILURE DIAGNOSTICS ===\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )


# ═══════════════════════════════════════════════════════════════
# C. Timeseries dumps
# ═══════════════════════════════════════════════════════════════

_TIMESERIES_FIELDS = {
    "capital_scale.csv": ("cycle", "capital_scale"),
    "deploy_ratio.csv": ("cycle", "deploy_ratio"),
    "overcommit.csv": ("cycle", "overcommit_factor", "target_notional",
                       "total_notional"),
    "flip_rate.csv": ("cycle", "delta_capital_scale",
                      "rolling_flip_rate_100", "flip_count_cumulative"),
}


def dump_timeseries(
    out_dir: str, results: list[V4SeedResult],
) -> list[str]:
    """Write the four required CSV files per (scenario, seed) run into
    out_dir / <scenario> / <seed> /. Returns the list of written paths.

    Every row includes `cycle`. Float fields are rendered with full
    precision so downstream analysis is not lossy; None renders as
    empty (so csv consumers see a NULL, not the string "None").
    """
    written: list[str] = []
    os.makedirs(out_dir, exist_ok=True)
    for r in results:
        subdir = os.path.join(out_dir, r.scenario, f"seed_{r.seed}")
        os.makedirs(subdir, exist_ok=True)
        for fname, cols in _TIMESERIES_FIELDS.items():
            path = os.path.join(subdir, fname)
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for snap in r.snapshots:
                    row = []
                    for c in cols:
                        v = getattr(snap, c, None)
                        row.append("" if v is None else v)
                    w.writerow(row)
            written.append(path)
        # Also dump a full-schema snapshot so downstream consumers that
        # want more than the four headline files have the raw record.
        full_path = os.path.join(subdir, "full_snapshots.jsonl")
        with open(full_path, "w") as f:
            for snap in r.snapshots:
                f.write(json.dumps(snap.to_row()) + "\n")
        written.append(full_path)
    return written


# ═══════════════════════════════════════════════════════════════
# Full-run JSON report — machine-readable overall result
# ═══════════════════════════════════════════════════════════════

def build_json_report(
    verdicts: list[V4ScenarioVerdict], cycles: int,
) -> dict:
    """Machine-readable report. Used by CI / downstream analysis."""
    return {
        "version": "4.0",
        "cycles_per_run": cycles,
        "overall": (
            "PASS" if all(v.verdict == "PASS" for v in verdicts) else "FAIL"
        ),
        "scenarios": [
            {
                "scenario": v.scenario,
                "seeds": v.seeds,
                "verdict": v.verdict,
                "inv3": v.inv3_all_pass,
                "inv5": v.inv5_all_pass,
                "inv7": v.inv7_all_pass,
                "per_seed": [
                    {
                        "seed": sr.seed,
                        "invariants": {
                            name: inv.to_dict()
                            for name, inv in sr.invariants.items()
                        },
                    }
                    for sr in v.per_seed
                ],
            }
            for v in verdicts
        ],
    }
