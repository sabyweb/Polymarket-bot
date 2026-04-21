"""simulation/report_v2.py — Profit-Max Enforcement audit.

Stricter than report.py's v1 directional-correctness check.
Any single criterion failure -> SYSTEM = FAIL. No partial credit.

Criteria (spec v2 §3-§5):
  A. stable_optimal deploy_ratio >= 0.80 (fail < 0.75)
  B. under_deployed max(capital_scale) >= 1.10 (fail if never > 1.05)
  C. ALL scenarios: efficiency_slope >= 0 AND final_eff >= initial_eff
  D. reward_per_dollar(stable_optimal) > over_aggressive AND regime_shift
  E. avg(deploy_ratio) across ALL cycles of ALL scenarios >= 0.70 (fail < 0.65)
  F. cap_scale delta: stable>=0, under>0, over<0
  G. stable_optimal capture_ratio >= 0.60 (fail < 0.50)
  + risk constraints (cycle/post-run invariants)
  + convergence: capital_scale stable within 5% over final 100 cycles
  + probe effectiveness: unobservable here (learning_state_history does not
    expose per-cycle probe markers). Reported explicitly as N/A.
"""

from __future__ import annotations
from typing import Optional

from .engine import SimulationResult
from .market_env import _BASE
from .runner import N_SYNTHETIC_MARKETS, TOTAL_CAPITAL


# Theoretical per-cycle reward ceiling per scenario.
# Env pays ~reward_rate per deployed market per cycle (before jitter).
# With N_SYNTHETIC_MARKETS markets the max per-cycle payout = N * reward_rate.
def _max_reward_per_cycle(scenario: str) -> float:
    if scenario == "regime_shift":
        r1 = _BASE["stable_optimal"]["reward_rate"]
        r2 = _BASE["over_aggressive"]["reward_rate"]
        return N_SYNTHETIC_MARKETS * (r1 + r2) / 2.0
    base = _BASE.get(scenario)
    if base is None:
        return 0.0
    return N_SYNTHETIC_MARKETS * base["reward_rate"]


def _per_scenario_metrics(r: SimulationResult) -> dict:
    hist = r.metrics.history()
    cycles = len(hist)
    if cycles == 0:
        return {"scenario": r.scenario, "cycles": 0}

    budget = hist[0].total_capital_budget or TOTAL_CAPITAL

    # Per-cycle deploy ratios.
    deploy_ratios = [
        (h.capital_deployed / budget) if budget > 0 else 0.0 for h in hist
    ]
    deploy_ratio_avg = sum(deploy_ratios) / len(deploy_ratios)
    back_deploy = deploy_ratios[cycles // 2:]
    deploy_ratio_back = (
        sum(back_deploy) / len(back_deploy) if back_deploy else 0.0
    )

    # reward_per_dollar over the back half (steady state).
    back = hist[cycles // 2:]
    back_rew = sum(h.reward for h in back)
    back_cap = sum(h.capital_deployed for h in back)
    rpd_back = back_rew / back_cap if back_cap > 0 else 0.0

    all_rew = sum(h.reward for h in hist)
    all_cap = sum(h.capital_deployed for h in hist)
    rpd_all = all_rew / all_cap if all_cap > 0 else 0.0

    eff_slope = r.metrics.rolling_trend_slope(
        "reward_efficiency", window=min(50, cycles),
    )

    eff_series = r.metrics.series("reward_efficiency")
    if len(eff_series) >= 40:
        initial_eff = sum(eff_series[:20]) / 20.0
        final_eff = sum(eff_series[-20:]) / 20.0
    elif len(eff_series) >= 4:
        n = len(eff_series) // 2
        initial_eff = sum(eff_series[:n]) / n
        final_eff = sum(eff_series[-n:]) / n
    else:
        initial_eff = None
        final_eff = None

    cap_scales = [
        float(h.learning_state.get("capital_scale", 1.0)) for h in hist
    ]
    cap_start = cap_scales[0]
    cap_end = cap_scales[-1]
    cap_max = max(cap_scales)
    cap_delta = cap_end - cap_start

    max_total = _max_reward_per_cycle(r.scenario) * cycles
    capture = r.cumulative_reward / max_total if max_total > 0 else 0.0

    # Convergence: rel range of capital_scale over last 100 cycles.
    CONV_WIN = 100
    if cycles >= CONV_WIN:
        tail = cap_scales[-CONV_WIN:]
        tmean = sum(tail) / len(tail)
        trange = max(tail) - min(tail)
        rel_range = trange / tmean if tmean > 0 else float("inf")
    else:
        rel_range = None

    return {
        "scenario": r.scenario,
        "cycles": cycles,
        "deploy_ratio_avg": deploy_ratio_avg,
        "deploy_ratio_back_half": deploy_ratio_back,
        "reward_per_dollar_back": rpd_back,
        "reward_per_dollar_all": rpd_all,
        "efficiency_slope": eff_slope,
        "initial_efficiency": initial_eff,
        "final_efficiency": final_eff,
        "capital_scale_start": cap_start,
        "capital_scale_end": cap_end,
        "capital_scale_max": cap_max,
        "capital_scale_delta": cap_delta,
        "capture_ratio": capture,
        "cumulative_reward": r.cumulative_reward,
        "cumulative_loss": r.cumulative_loss,
        "max_possible_reward": max_total,
        "convergence_rel_range_last100": rel_range,
        "n_per_cycle_violations": len(r.per_cycle_violations),
        "n_post_run_violations": len(r.post_run_violations),
        "post_run_violation_names": [v.name for v in r.post_run_violations],
    }


# Pass/fail thresholds, quoted from the v2 spec.
A_FAIL = 0.75
A_STRICT = 0.80
B_FAIL_LE = 1.05
B_STRICT = 1.10
E_FAIL = 0.65
E_STRICT = 0.70
G_FAIL = 0.50
G_STRICT = 0.60
CONV_THRESHOLD = 0.05


def _evaluate_seed(results: list[SimulationResult]) -> dict:
    """Apply the hard v2 criteria to one seed's results."""
    by_scen = {r.scenario: _per_scenario_metrics(r) for r in results}
    failures: list[str] = []
    per_scenario_results: dict = {}

    # Initialise per-scenario PASS; will be flipped on any failing gate.
    for s in by_scen:
        per_scenario_results[s] = {"result": "PASS", "reasons": []}

    def _fail(scenario: str, code: str, detail: str) -> None:
        failures.append(f"[{scenario}] {code}: {detail}")
        bucket = per_scenario_results.setdefault(
            scenario, {"result": "PASS", "reasons": []},
        )
        bucket["result"] = "FAIL"
        bucket["reasons"].append(f"{code}: {detail}")

    # A. Capital utilization — stable_optimal
    so = by_scen.get("stable_optimal", {})
    so_dep = so.get("deploy_ratio_back_half", 0.0)
    if so_dep < A_FAIL:
        _fail(
            "stable_optimal", "A_deploy_ratio_below_fail",
            f"back-half deploy_ratio={so_dep:.2%} < {A_FAIL:.0%}",
        )
    elif so_dep < A_STRICT:
        _fail(
            "stable_optimal", "A_deploy_ratio_below_strict",
            f"back-half deploy_ratio={so_dep:.2%} < {A_STRICT:.0%}",
        )

    # B. Under-deployed expansion
    ud = by_scen.get("under_deployed", {})
    ud_max = ud.get("capital_scale_max", 0.0)
    if ud_max <= B_FAIL_LE:
        _fail(
            "under_deployed", "B_capital_scale_never_expanded",
            f"max(capital_scale)={ud_max:.3f} <= {B_FAIL_LE}",
        )
    elif ud_max < B_STRICT:
        _fail(
            "under_deployed", "B_capital_scale_below_strict",
            f"max(capital_scale)={ud_max:.3f} < {B_STRICT}",
        )

    # C. Efficiency improvement — ALL scenarios
    for scen, m in by_scen.items():
        slope = m.get("efficiency_slope")
        init_e = m.get("initial_efficiency")
        final_e = m.get("final_efficiency")
        if slope is None or init_e is None or final_e is None:
            _fail(
                scen, "C_efficiency_insufficient_data",
                "not enough reward_efficiency samples",
            )
            continue
        if slope < 0:
            _fail(
                scen, "C_efficiency_slope_negative",
                f"slope={slope:.3e} < 0",
            )
        if final_e < init_e - 1e-9:
            _fail(
                scen, "C_efficiency_regressed",
                f"final={final_e:.4f} < initial={init_e:.4f}",
            )

    # D. Optimal scenario dominance
    so_rpd = so.get("reward_per_dollar_back", 0.0)
    oa_rpd = by_scen.get("over_aggressive", {}).get(
        "reward_per_dollar_back", 0.0,
    )
    rs_rpd = by_scen.get("regime_shift", {}).get(
        "reward_per_dollar_back", 0.0,
    )
    if so_rpd <= oa_rpd:
        _fail(
            "stable_optimal", "D_not_dominating_over_aggressive",
            f"rpd(stable)={so_rpd:.4f} !> rpd(over_aggr)={oa_rpd:.4f}",
        )
    if so_rpd <= rs_rpd:
        _fail(
            "stable_optimal", "D_not_dominating_regime_shift",
            f"rpd(stable)={so_rpd:.4f} !> rpd(regime_shift)={rs_rpd:.4f}",
        )

    # E. Average deploy ratio across ALL cycles of ALL scenarios
    ratios = [m.get("deploy_ratio_avg", 0.0) for m in by_scen.values()]
    avg_deploy = sum(ratios) / len(ratios) if ratios else 0.0
    if avg_deploy < E_FAIL:
        _fail(
            "GLOBAL", "E_avg_deploy_below_fail",
            f"avg_deploy_ratio={avg_deploy:.2%} < {E_FAIL:.0%}",
        )
    elif avg_deploy < E_STRICT:
        _fail(
            "GLOBAL", "E_avg_deploy_below_strict",
            f"avg_deploy_ratio={avg_deploy:.2%} < {E_STRICT:.0%}",
        )

    # F. Learning effectiveness (cap_scale delta direction)
    expectations = {
        "stable_optimal": (">=", 0.0, "must not contract"),
        "under_deployed": (">", 0.0, "must expand"),
        "over_aggressive": ("<", 0.0, "must contract"),
    }
    for scen, (op, val, why) in expectations.items():
        m = by_scen.get(scen, {})
        delta = m.get("capital_scale_delta")
        if delta is None:
            _fail(scen, "F_learning_no_delta", "no capital_scale delta")
            continue
        ok = (
            (op == ">=" and delta >= val - 1e-9)
            or (op == ">" and delta > val + 1e-6)
            or (op == "<" and delta < val - 1e-6)
        )
        if not ok:
            _fail(
                scen, "F_learning_wrong_direction",
                f"delta={delta:+.3f} violates '{op} {val}' ({why})",
            )

    # G. Capture ratio (stable_optimal)
    so_cap = so.get("capture_ratio", 0.0)
    if so_cap < G_FAIL:
        _fail(
            "stable_optimal", "G_capture_ratio_below_fail",
            f"capture_ratio={so_cap:.2%} < {G_FAIL:.0%}",
        )
    elif so_cap < G_STRICT:
        _fail(
            "stable_optimal", "G_capture_ratio_below_strict",
            f"capture_ratio={so_cap:.2%} < {G_STRICT:.0%}",
        )

    # Risk constraints — any per-cycle or post-run invariant violation fails.
    for r in results:
        if r.per_cycle_violations:
            names = [v.name for v in r.per_cycle_violations[:3]]
            _fail(
                r.scenario, "RISK_per_cycle_violations",
                f"{len(r.per_cycle_violations)} viol: {','.join(names)}",
            )
        if r.post_run_violations:
            names = [v.name for v in r.post_run_violations]
            _fail(
                r.scenario, "RISK_post_run_violations",
                f"{','.join(names)}",
            )

    # Convergence — capital_scale stable within 5% over final 100 cycles.
    for scen, m in by_scen.items():
        rr = m.get("convergence_rel_range_last100")
        if rr is None:
            continue  # too short to evaluate
        if rr >= CONV_THRESHOLD:
            _fail(
                scen, "CONV_not_converged",
                f"last-100 capital_scale rel_range={rr:.1%} "
                f">= {CONV_THRESHOLD:.0%}",
            )

    status = "PASS" if not failures else "FAIL"
    return {
        "status": status,
        "per_scenario_metrics": by_scen,
        "per_scenario_results": per_scenario_results,
        "avg_deploy_ratio": avg_deploy,
        "failures": failures,
    }


def build_report_v2(results_by_seed: dict) -> dict:
    """Build v2 report across multiple seeds.

    results_by_seed: {seed: list[SimulationResult]} — one result per scenario.
    """
    per_seed_reports: dict = {}
    all_failures: list[str] = []

    for seed in sorted(results_by_seed.keys()):
        seed_report = _evaluate_seed(results_by_seed[seed])
        per_seed_reports[seed] = seed_report
        if seed_report["status"] == "FAIL":
            for f in seed_report["failures"]:
                all_failures.append(f"seed={seed} | {f}")

    multi_seed_pass = all(
        r["status"] == "PASS" for r in per_seed_reports.values()
    )
    status = "PASS" if multi_seed_pass else "FAIL"

    # Aggregate snapshot
    total_reward = 0.0
    total_loss = 0.0
    for seed_report in per_seed_reports.values():
        for m in seed_report["per_scenario_metrics"].values():
            total_reward += m.get("cumulative_reward", 0.0)
            total_loss += m.get("cumulative_loss", 0.0)

    return {
        "status": status,
        "per_seed": per_seed_reports,
        "multi_seed_pass": multi_seed_pass,
        "failures": all_failures,
        "global_totals": {
            "total_reward": total_reward,
            "total_loss": total_loss,
        },
        "notes": {
            "probe_effectiveness": (
                "N/A — LearningStep.metrics does not surface a per-cycle "
                "probe marker, and probe_cycle ids aren't persisted in "
                "learning_state_history. Not evaluated here; reported "
                "explicitly to avoid silent pass."
            ),
        },
    }


def print_report_v2(report: dict) -> None:
    print()
    print("=" * 60)
    print("=== FINAL AUDIT V2 REPORT ===")
    print("=" * 60)
    print()
    print(f"STATUS: {report.get('status', 'UNKNOWN')}")
    print()

    for seed in sorted(report.get("per_seed", {}).keys()):
        sr = report["per_seed"][seed]
        status = sr["status"]
        print(f"--- seed={seed} --- status={status}")
        print()
        print("Per Scenario:")
        print("-" * 40)
        for scen in (
            "stable_optimal", "under_deployed", "over_aggressive",
            "high_reward_fake", "regime_shift",
        ):
            m = sr["per_scenario_metrics"].get(scen)
            res = sr["per_scenario_results"].get(
                scen, {"result": "N/A", "reasons": []},
            )
            if not m:
                print(f"  {scen}: (no data)   RESULT: N/A")
                continue
            print(f"  {scen}:")
            if scen == "stable_optimal":
                print(
                    f"    deploy_ratio (back-half) : "
                    f"{m['deploy_ratio_back_half']:.2%}"
                )
                print(
                    f"    reward_per_dollar        : "
                    f"{m['reward_per_dollar_back']:.5f}"
                )
                slope = m.get("efficiency_slope")
                slope_s = f"{slope:.3e}" if slope is not None else "n/a"
                print(f"    efficiency_slope         : {slope_s}")
                print(
                    f"    capture_ratio            : "
                    f"{m['capture_ratio']:.2%} "
                    f"({m['cumulative_reward']:.1f}/"
                    f"{m['max_possible_reward']:.1f})"
                )
                print(
                    f"    capital_scale_delta      : "
                    f"{m['capital_scale_delta']:+.3f}"
                )
            elif scen == "under_deployed":
                print(
                    f"    capital_scale_max        : "
                    f"{m['capital_scale_max']:.3f}"
                )
                print(
                    f"    capital_scale_delta      : "
                    f"{m['capital_scale_delta']:+.3f}"
                )
                slope = m.get("efficiency_slope")
                slope_s = f"{slope:.3e}" if slope is not None else "n/a"
                print(f"    efficiency_slope         : {slope_s}")
            elif scen == "over_aggressive":
                print(
                    f"    capital_scale_delta      : "
                    f"{m['capital_scale_delta']:+.3f}"
                )
                slope = m.get("efficiency_slope")
                slope_s = f"{slope:.3e}" if slope is not None else "n/a"
                print(f"    efficiency_slope         : {slope_s}")
                print(
                    f"    reward_per_dollar        : "
                    f"{m['reward_per_dollar_back']:.5f}"
                )
            else:
                print(
                    f"    capital_scale_delta      : "
                    f"{m['capital_scale_delta']:+.3f}"
                )
                slope = m.get("efficiency_slope")
                slope_s = f"{slope:.3e}" if slope is not None else "n/a"
                print(f"    efficiency_slope         : {slope_s}")
                print(
                    f"    reward_per_dollar        : "
                    f"{m['reward_per_dollar_back']:.5f}"
                )
            print(f"    RESULT: {res['result']}")
            for reason in res["reasons"]:
                print(f"      - {reason}")
        print()
        print("Global:")
        print("-" * 40)
        print(
            f"  avg_deploy_ratio : {sr['avg_deploy_ratio']:.2%}"
        )
        print(f"  seed_status      : {status}")
        print()

    print("=" * 60)
    print(f"multi_seed_pass: "
          f"{'YES' if report.get('multi_seed_pass') else 'NO'}")
    g = report.get("global_totals", {})
    print(f"total_reward (all seeds × scenarios): ${g.get('total_reward', 0):.2f}")
    print(f"total_loss   (all seeds × scenarios): ${g.get('total_loss', 0):.2f}")
    print()
    notes = report.get("notes", {})
    if notes:
        print("Notes:")
        for k, v in notes.items():
            print(f"  - {k}: {v}")
        print()
    if report.get("failures"):
        print("Failure list:")
        for f in report["failures"]:
            print(f"  - {f}")
        print()
    print(f"FINAL VERDICT: {report.get('status', 'UNKNOWN')}")
    print("=" * 60)
