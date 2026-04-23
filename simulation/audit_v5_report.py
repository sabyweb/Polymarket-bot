"""simulation/audit_v5_report.py — output generation for Audit V5.

Produces:

  A. Summary table           | Scenario | INV3_new | INV5_new | INV7 | Verdict |
  B. Failure diagnostics     [INV3_new FAIL] / [INV5_new FAIL] / [INV7 FAIL]
                             blocks keyed exactly to spec section 3.5.
  C. Timeseries dumps        per (scenario, seed) — adds expected_util.csv
                             and coverage_ratio.csv to the V4 dump set.
  D. Machine-readable JSON   version="5.0", overall verdict, per-scenario
                             per-seed invariant payloads.

All V5-specific data structures live in this module; V4 code is
unchanged. Downstream analysis can import V4 or V5 in isolation.
"""

from __future__ import annotations
import csv
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .audit_v4_metrics import V4CycleSnapshot
from .audit_v5_invariants import (
    V5InvariantResult,
    compute_expected_util, compute_coverage_ratio,
)


# ═══════════════════════════════════════════════════════════════
# Per-(scenario, seed) + per-scenario aggregation types
# ═══════════════════════════════════════════════════════════════

@dataclass
class V5SeedResult:
    """One (scenario, seed) simulation run under V5 invariants."""
    scenario: str
    seed: int
    cycles: int
    snapshots: list[V4CycleSnapshot]
    invariants: dict[str, V5InvariantResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.invariants.values())


@dataclass
class V5ScenarioVerdict:
    """Aggregate verdict for one scenario across all seeds."""
    scenario: str
    seeds: list[int]
    inv3_new_all_pass: bool
    inv5_new_all_pass: bool
    inv7_all_pass: bool
    per_seed: list[V5SeedResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return "PASS" if (
            self.inv3_new_all_pass
            and self.inv5_new_all_pass
            and self.inv7_all_pass
        ) else "FAIL"


def build_scenario_verdicts_v5(
    results: list[V5SeedResult],
) -> list[V5ScenarioVerdict]:
    """Group seeds by scenario, preserving the input order so CLI
    argument order is reflected in the summary table."""
    buckets: dict[str, list[V5SeedResult]] = {}
    order: list[str] = []
    for r in results:
        if r.scenario not in buckets:
            buckets[r.scenario] = []
            order.append(r.scenario)
        buckets[r.scenario].append(r)

    out: list[V5ScenarioVerdict] = []
    for scenario in order:
        seed_results = buckets[scenario]
        seeds = [r.seed for r in seed_results]
        inv3_new = all(
            r.invariants["INV3_new"].passed for r in seed_results
        )
        inv5_new = all(
            r.invariants["INV5_new"].passed for r in seed_results
        )
        inv7 = all(r.invariants["INV7"].passed for r in seed_results)
        out.append(V5ScenarioVerdict(
            scenario=scenario,
            seeds=seeds,
            inv3_new_all_pass=inv3_new,
            inv5_new_all_pass=inv5_new,
            inv7_all_pass=inv7,
            per_seed=seed_results,
        ))
    return out


# ═══════════════════════════════════════════════════════════════
# A. Summary table
# ═══════════════════════════════════════════════════════════════

def emit_summary_table_v5(verdicts: list[V5ScenarioVerdict]) -> str:
    """Return the spec-format Markdown summary table.

    Columns (per spec §4.1): | Scenario | INV3_new | INV5_new | INV7 | Verdict |
    """
    lines = [
        "=== AUDIT V5 — SCENARIO SUMMARY ===",
        "",
        "| Scenario              | INV3_new | INV5_new | INV7 | Verdict |",
        "|-----------------------|----------|----------|------|---------|",
    ]
    for v in verdicts:
        lines.append(
            f"| {v.scenario:<21} | "
            f"{'PASS' if v.inv3_new_all_pass else 'FAIL':<8} | "
            f"{'PASS' if v.inv5_new_all_pass else 'FAIL':<8} | "
            f"{'PASS' if v.inv7_all_pass else 'FAIL':<4} | "
            f"{v.verdict:<7} |"
        )
    lines.append("")
    all_pass = all(v.verdict == "PASS" for v in verdicts)
    lines.append(f"Overall: {'PASS' if all_pass else 'FAIL'}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# B. Failure diagnostics (spec §3.5 exact format)
# ═══════════════════════════════════════════════════════════════

# Per-invariant headline metric order for the diagnostics block. Only
# these keys are emitted (in this order) after scenario/seed/cycles,
# matching the spec's template exactly.
_DIAG_METRIC_ORDER: dict[str, tuple[str, ...]] = {
    "INV3_new": (
        "expected_util", "total_expected_capital", "total_capital",
    ),
    "INV5_new": (
        "coverage_ratio", "active_markets", "total_markets",
    ),
    # INV7 is carried through unchanged; we emit its core metrics so
    # diagnostics remain greppable without reproducing V4's rich set.
    "INV7": (
        "max_flip_rate_100", "sustained_alt_runs", "breached_cycles",
    ),
}


def emit_failure_diagnostics_v5(
    verdicts: list[V5ScenarioVerdict],
) -> str:
    """Emit the `[INVx_new FAIL]` blocks for every failed invariant.

    One block per (scenario, seed, failing-cycle-range) tuple. When an
    invariant has no explicit failing ranges (e.g., INV7 aggregate
    failure), emit a single block with the full post-warmup window.
    """
    blocks: list[str] = []
    for v in verdicts:
        for seed_result in v.per_seed:
            for inv_name, inv_result in seed_result.invariants.items():
                if inv_result.passed:
                    continue
                ranges = inv_result.failing_cycles or [
                    _full_post_warmup_range(seed_result)
                ]
                for start, end in ranges:
                    lines = [f"[{inv_name} FAIL]"]
                    lines.append(f"scenario={v.scenario}")
                    lines.append(f"seed={seed_result.seed}")
                    lines.append(f"cycles={start}-{end}")
                    for key in _DIAG_METRIC_ORDER.get(inv_name, ()):
                        if key in inv_result.metric_values:
                            lines.append(
                                f"{key}={inv_result.metric_values[key]}"
                            )
                    if inv_result.reason:
                        lines.append(f"reason={inv_result.reason}")
                    blocks.append("\n".join(lines))
    if not blocks:
        return "No invariant failures.\n"
    return (
        "=== AUDIT V5 — FAILURE DIAGNOSTICS ===\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def _full_post_warmup_range(
    seed_result: V5SeedResult,
) -> tuple[int, int]:
    """Return (warmup+1, last_cycle) for a V5SeedResult. Used when an
    invariant failed but produced no per-cycle failing ranges — e.g.,
    no-samples / divide-by-zero cases."""
    if not seed_result.snapshots:
        return (0, 0)
    last = seed_result.snapshots[-1].cycle
    # warmup cutoff is cycle > 100 → post-warmup starts at 101
    from .audit_v5_invariants import WARMUP_CUTOFF  # re-exported
    return (WARMUP_CUTOFF + 1, last)


# ═══════════════════════════════════════════════════════════════
# C. Timeseries dumps
# ═══════════════════════════════════════════════════════════════

# V5 adds expected_util and coverage_ratio on top of the V4 column set.
_V5_TIMESERIES_FIELDS: dict[str, tuple[str, ...]] = {
    # Re-use the V4 headers for continuity — downstream analysis that
    # already consumes these keeps working against the V5 output.
    "capital_scale.csv":   ("cycle", "capital_scale"),
    "flip_rate.csv":       ("cycle", "delta_capital_scale",
                            "rolling_flip_rate_100",
                            "flip_count_cumulative"),
    # New V5 columns — the two quantities the new invariants consume.
    "expected_util.csv":   ("cycle", "expected_util",
                            "expected_capital", "total_capital"),
    "coverage_ratio.csv":  ("cycle", "coverage_ratio",
                            "active_markets",
                            "number_of_deployed_markets",
                            "min_size_alloc_count"),
}


def dump_timeseries_v5(
    out_dir: str, results: list[V5SeedResult],
) -> list[str]:
    """Write per (scenario, seed) CSVs + a full JSONL snapshot.

    expected_util and coverage_ratio are derived on the fly from each
    snapshot so we do not have to mutate V4CycleSnapshot. Rows with a
    None derived value emit an empty cell (CSV NULL)."""
    written: list[str] = []
    os.makedirs(out_dir, exist_ok=True)
    for r in results:
        subdir = os.path.join(out_dir, r.scenario, f"seed_{r.seed}")
        os.makedirs(subdir, exist_ok=True)

        for fname, cols in _V5_TIMESERIES_FIELDS.items():
            path = os.path.join(subdir, fname)
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for snap in r.snapshots:
                    w.writerow(
                        _render_row(snap, cols)
                    )
            written.append(path)

        # Full-schema JSONL: V4 snapshot + derived V5 metrics per cycle.
        full_path = os.path.join(subdir, "full_snapshots.jsonl")
        with open(full_path, "w") as f:
            for snap in r.snapshots:
                row = snap.to_row()
                row["expected_util"] = compute_expected_util(snap)
                row["coverage_ratio"] = compute_coverage_ratio(snap)
                row["active_markets"] = _active_markets(snap)
                f.write(json.dumps(row) + "\n")
        written.append(full_path)
    return written


def _render_row(
    snap: V4CycleSnapshot, cols: tuple[str, ...],
) -> list:
    """Render a CSV row with V5-derived fallbacks for the two new
    columns."""
    row = []
    for c in cols:
        if c == "expected_util":
            v = compute_expected_util(snap)
        elif c == "coverage_ratio":
            v = compute_coverage_ratio(snap)
        elif c == "active_markets":
            v = _active_markets(snap)
        else:
            v = getattr(snap, c, None)
        row.append("" if v is None else v)
    return row


def _active_markets(snap: V4CycleSnapshot) -> int:
    total = int(snap.number_of_deployed_markets or 0)
    at_min = int(snap.min_size_alloc_count or 0)
    return max(0, total - at_min)


# ═══════════════════════════════════════════════════════════════
# D. Machine-readable JSON report
# ═══════════════════════════════════════════════════════════════

def build_json_report_v5(
    verdicts: list[V5ScenarioVerdict], cycles: int,
) -> dict:
    """Full machine-readable verdict payload. Consumed by CI and
    downstream analysis tools."""
    return {
        "version": "5.0",
        "cycles_per_run": cycles,
        "overall": (
            "PASS" if all(v.verdict == "PASS" for v in verdicts) else "FAIL"
        ),
        "scenarios": [
            {
                "scenario": v.scenario,
                "seeds": v.seeds,
                "verdict": v.verdict,
                "inv3_new": v.inv3_new_all_pass,
                "inv5_new": v.inv5_new_all_pass,
                "inv7":     v.inv7_all_pass,
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
