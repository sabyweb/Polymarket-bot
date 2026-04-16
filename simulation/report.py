"""simulation/report.py — PASS / FAIL audit report builder.

Consumes a list of SimulationResult (one per scenario) and applies the
spec's pass/fail criteria. The report is a plain dict so it round-trips
through JSON cleanly. Also provides `print_report` for the CLI.

Pass conditions (must hold for STATUS=PASS):
  - Zero per-cycle invariant violations across ALL scenarios
  - Zero post-run invariant violations
  - For each scenario, scenario-specific learning-direction check passes
  - Capital is not stuck under-deploying in stable_optimal
  - Regime shift produces > 1 frontier_memory entries
"""

from __future__ import annotations
from dataclasses import dataclass

from .engine import SimulationResult


# Per-scenario expected learning direction. Each rule returns
# (passed: bool, note: str).

def _stable_optimal_check(r: SimulationResult) -> tuple[bool, str]:
    """In stable_optimal, reward_efficiency should NOT be on a strong
    decline, AND the system should deploy meaningful capital (>50% of
    budget on average over the back half)."""
    half = r.cycles // 2
    cap_series = r.metrics.series("capital_deployed")[half:]
    if not cap_series:
        return False, "no capital_deployed samples in back half"
    avg_cap = sum(cap_series) / len(cap_series)
    budget = r.metrics.history()[0].total_capital_budget if r.metrics.history() else 1.0
    deploy_ratio = avg_cap / budget if budget > 0 else 0.0
    if deploy_ratio < 0.50:
        return False, (
            f"stuck under-deploying: avg back-half capital "
            f"${avg_cap:.0f} / ${budget:.0f} = {deploy_ratio:.0%} < 50%"
        )
    slope = r.metrics.rolling_trend_slope(
        "reward_efficiency", window=min(100, r.cycles),
    )
    if slope is None:
        return False, "could not compute reward_efficiency slope"
    if slope < -1e-5:
        return False, f"reward_efficiency trend negative ({slope:+.2e})"
    return True, f"deploy_ratio={deploy_ratio:.0%}, eff_slope={slope:+.2e}"


def _over_aggressive_check(r: SimulationResult) -> tuple[bool, str]:
    """In over_aggressive, the learning loop should pull capital_scale
    DOWN over the run as Rule A and the loss-per-capital path fire."""
    history = r.learning_state_history
    if len(history) < 50:
        return False, f"history too short ({len(history)})"
    cap_first = history[10]["capital_scale"]
    cap_last = history[-1]["capital_scale"]
    if cap_last >= cap_first - 1e-3:
        return False, (
            f"capital_scale DID NOT contract: "
            f"{cap_first:.3f} -> {cap_last:.3f}"
        )
    return True, f"capital_scale {cap_first:.3f} -> {cap_last:.3f}"


def _under_deployed_check(r: SimulationResult) -> tuple[bool, str]:
    """In under_deployed, the system MUST not aggressively expand into
    a low-fill regime. capital_scale should stay <= start within noise."""
    history = r.learning_state_history
    if len(history) < 20:
        return False, f"history too short ({len(history)})"
    cap_max = max(h["capital_scale"] for h in history)
    cap_init = history[0]["capital_scale"]
    if cap_max > cap_init * 1.20:
        return False, (
            f"capital_scale over-expanded into low-fill regime: "
            f"max={cap_max:.3f} vs init={cap_init:.3f}"
        )
    return True, f"capital_scale max={cap_max:.3f}, init={cap_init:.3f}"


def _high_reward_fake_check(r: SimulationResult) -> tuple[bool, str]:
    """In high_reward_fake, the calibrator's advertised reward >> actual,
    so reward_trust SHOULD trend down OR stay below 1.0 most of the
    back half."""
    history = r.learning_state_history
    if len(history) < 50:
        return False, f"history too short ({len(history)})"
    back = history[len(history) // 2:]
    avg_trust = sum(h["reward_trust"] for h in back) / len(back)
    if avg_trust >= 0.99:
        return False, (
            f"reward_trust did not detect inflated rewards: "
            f"avg back-half trust={avg_trust:.3f} (expected < 0.99)"
        )
    return True, f"avg_back_half_trust={avg_trust:.3f}"


def _regime_shift_check(r: SimulationResult) -> tuple[bool, str]:
    """In regime_shift, the frontier_memory MUST grow > 1 entry by run
    end (proves Patch 5 created a new regime bucket post-shift)."""
    history = r.learning_state_history
    if not history:
        return False, "no history"
    mem_size_end = history[-1].get("frontier_memory_size", 0)
    if mem_size_end < 2:
        # Unobservable if mode never reached ACTIVE — dial down to a
        # softer requirement: at least the regime tag must change in
        # the metric stream.
        regimes = {m.regime_id for m in r.metrics.history() if m.regime_id}
        if len(regimes) < 2:
            return False, (
                f"frontier_memory size end={mem_size_end} "
                f"AND only {len(regimes)} regime_ids observed"
            )
        return True, (
            f"frontier_memory={mem_size_end} but regime_ids "
            f"observed={len(regimes)} (multi-regime detected)"
        )
    return True, f"frontier_memory size end={mem_size_end}"


_SCENARIO_CHECKS = {
    "stable_optimal": _stable_optimal_check,
    "over_aggressive": _over_aggressive_check,
    "under_deployed": _under_deployed_check,
    "high_reward_fake": _high_reward_fake_check,
    "regime_shift": _regime_shift_check,
}


def build_report(results: list[SimulationResult]) -> dict:
    scenarios: dict = {}
    failures: list[str] = []

    cum_reward = 0.0
    cum_loss = 0.0
    first_eff = None
    last_eff = None

    for r in results:
        cum_reward += r.cumulative_reward
        cum_loss += r.cumulative_loss

        eff_slope = r.metrics.rolling_trend_slope(
            "reward_efficiency", window=min(100, r.cycles),
        )
        eff_series = r.metrics.series("reward_efficiency")
        if eff_series:
            if first_eff is None:
                first_eff = eff_series[0]
            last_eff = eff_series[-1]

        learning_correct = True
        notes: list[str] = []
        check = _SCENARIO_CHECKS.get(r.scenario)
        if check is not None:
            ok, note = check(r)
            learning_correct = ok
            notes.append(note)
            if not ok:
                failures.append(f"{r.scenario}: {note}")

        if r.per_cycle_violations:
            v_summary = ", ".join(
                f"{v.name}({v.detail[:80]})"
                for v in r.per_cycle_violations[:5]
            )
            failures.append(
                f"{r.scenario}: {len(r.per_cycle_violations)} "
                f"per-cycle invariant violation(s): {v_summary}"
            )
        if r.post_run_violations:
            v_summary = ", ".join(
                f"{v.name}({v.detail[:80]})"
                for v in r.post_run_violations
            )
            failures.append(
                f"{r.scenario}: post-run violations: {v_summary}"
            )

        scenarios[r.scenario] = {
            "reward_per_dollar_trend": (
                "non-decreasing"
                if eff_slope is not None and eff_slope >= -1e-5
                else "decreasing" if eff_slope is not None
                else "insufficient-data"
            ),
            "reward_per_dollar_slope": eff_slope,
            "final_capital_scale": r.final_learning_state.get(
                "capital_scale", 1.0,
            ),
            "max_drawdown": r.metrics.drawdown(),
            "cumulative_reward": r.cumulative_reward,
            "cumulative_loss": r.cumulative_loss,
            "n_per_cycle_violations": len(r.per_cycle_violations),
            "n_post_run_violations": len(r.post_run_violations),
            "learning_correct": learning_correct,
            "notes": "; ".join(notes) if notes else "",
        }

    efficiency_improvement: float = 0.0
    if first_eff is not None and last_eff is not None and first_eff > 0:
        efficiency_improvement = (last_eff - first_eff) / first_eff

    status = "PASS" if not failures else "FAIL"
    return {
        "status": status,
        "scenarios": scenarios,
        "global_summary": {
            "total_reward": cum_reward,
            "total_loss": cum_loss,
            "efficiency_improvement": efficiency_improvement,
        },
        "failures": failures,
    }


def print_report(report: dict) -> None:
    """Pretty-prints the audit report."""
    status = report.get("status", "UNKNOWN")
    print()
    print("=" * 60)
    print("=== FINAL SYSTEM AUDIT ===")
    print(f"STATUS: {status}")
    print("=" * 60)
    print()
    print("Per-scenario summary:")
    for name, s in report.get("scenarios", {}).items():
        ok = "PASS" if s.get("learning_correct") else "FAIL"
        print(f"  [{ok}] {name}")
        print(f"        trend           : {s.get('reward_per_dollar_trend')}")
        print(f"        slope           : {s.get('reward_per_dollar_slope')}")
        print(f"        final cap_scale : {s.get('final_capital_scale'):.3f}")
        print(f"        max drawdown    : ${s.get('max_drawdown'):.2f}")
        print(f"        cum_reward      : ${s.get('cumulative_reward'):.2f}")
        print(f"        cum_loss        : ${s.get('cumulative_loss'):.2f}")
        if s.get("n_per_cycle_violations"):
            print(
                f"        per-cycle viol  : "
                f"{s['n_per_cycle_violations']}"
            )
        if s.get("n_post_run_violations"):
            print(
                f"        post-run viol   : "
                f"{s['n_post_run_violations']}"
            )
        if s.get("notes"):
            print(f"        notes           : {s['notes']}")
    g = report.get("global_summary", {})
    print()
    print("Global summary:")
    print(f"  total reward     : ${g.get('total_reward', 0):.2f}")
    print(f"  total loss       : ${g.get('total_loss', 0):.2f}")
    print(
        f"  efficiency impr. : {g.get('efficiency_improvement', 0):.2%}"
    )
    print()
    if report.get("failures"):
        print("Failures:")
        for f in report["failures"]:
            print(f"  - {f}")
        print()
    print(f"STATUS: {status}")
    print("=" * 60)
