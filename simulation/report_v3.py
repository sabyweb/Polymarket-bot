"""simulation/report_v3.py — V3.1 audit: bootstrap-excluded, ACTIVE-aware.

V3.1 upgrade over V3:
  • Bootstrap exclusion: first `BOOTSTRAP_CYCLES` cycles are skipped
    when computing metrics (allows the learning gate to cross into
    ACTIVE and the FillModel to leave cold-start).
  • Overcommit invariant measured on ACTIVE cycles only — OFF/SHADOW
    legitimately keep overcommit = 1.0 by design.
  • Stable-utilisation gate replaced with "expected_capital positive
    and evolving" — reward farming operates at low fill probability, so
    a 70% utilisation threshold was a false negative.
  • Efficiency retention replaces slope (front→back ratio ≥ 0.70×).
  • Dominance measured on TOTAL REWARD, not per-expected-dollar, so
    contracted-portfolio arithmetic artifacts don't invert the check.
  • Exploration cap measured against DEPLOYABLE (overcommit-expanded)
    capital, matching production's gating.
  • Oscillation check kept strict — real signal.

No production code touched; all changes are audit-only.
"""

from __future__ import annotations
from typing import Optional

from .engine import SimulationResult
from .market_env import _BASE
from .runner import N_SYNTHETIC_MARKETS


# ── V3.1 audit thresholds ──────────────────────────────────────
BOOTSTRAP_CYCLES = 50           # V3.1 PART 1 — skip first N cycles
ACTIVE_MIN_CYCLES = 20          # V3.1 PART 2 — evaluation guard
OVERCOMMIT_MIN_ACTIVE = 1.5     # V3.1 PART 2 — ACTIVE-only avg threshold
EFF_RETENTION_FLOOR = 0.70      # V3.1 PART 4 — eff_back ≥ 0.70 × eff_front
EXPLORATION_MAX_FRAC_DEPL = 0.15  # V3.1 PART 6 — vs deployable_capital
EXP_CAP_HARD_CAP = 1.00         # V3 INV1 preserved
TAIL_RISK_MAX = 0.30            # V3 INV2 preserved
CAPTURE_RATIO_MIN = 0.70        # V3 G preserved
UNDER_DEPLOY_CAP_MIN = 1.10     # V3 D preserved

# Oscillation (unchanged from V3)
OSC_SIGN_FLIPS = 6
OSC_WIN = 20
OSC_WIN_LIMIT = 20

# Learning scalar clamps (ground truth from profit.learning)
CLAMP_AGGR = (0.30, 1.50)
CLAMP_CAP = (0.30, 1.20)
CLAMP_RISK = (1.00, 2.00)
CLAMP_TRUST = (0.50, 1.00)


def _theoretical_max_reward(scenario: str, cycles: int) -> float:
    """N_MARKETS × reward_rate × cycles — for the capture ratio check.
    Computed on the FULL cycle count (not bootstrap-trimmed) so we
    compare apples to apples with the actual reward sum."""
    if scenario == "regime_shift":
        r1 = _BASE["stable_optimal"]["reward_rate"]
        r2 = _BASE["over_aggressive"]["reward_rate"]
        per_cycle = N_SYNTHETIC_MARKETS * (r1 + r2) / 2.0
    else:
        base = _BASE.get(scenario)
        if base is None:
            return 0.0
        per_cycle = N_SYNTHETIC_MARKETS * base["reward_rate"]
    return per_cycle * cycles


def _mean(xs: list) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def _variance(xs: list) -> float:
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def _sign_change_windows(series: list[float], window: int = OSC_WIN,
                         sign_threshold: int = OSC_SIGN_FLIPS) -> int:
    """Max consecutive `window`-cycle windows with ≥ sign_threshold
    direction flips in the delta series."""
    if len(series) < window + 1:
        return 0
    deltas = [series[i] - series[i-1] for i in range(1, len(series))]
    max_consec = 0
    consec = 0
    for i in range(window, len(deltas) + 1):
        seg = deltas[i-window:i]
        flips = 0
        prev_sign = 0
        for d in seg:
            if abs(d) < 1e-6:
                continue
            s = 1 if d > 0 else -1
            if prev_sign != 0 and s != prev_sign:
                flips += 1
            prev_sign = s
        if flips >= sign_threshold:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0
    return max_consec


def _per_scenario_v31_metrics(r: SimulationResult) -> dict:
    """V3.1 — aggregate metrics with bootstrap exclusion + ACTIVE filter."""
    v3 = r.v3_per_cycle
    hist = r.metrics.history()
    cycles_total = len(v3)
    if cycles_total == 0:
        return {"scenario": r.scenario, "cycles_total": 0, "post_boot": 0}

    # V3.1 PART 1 — exclude first BOOTSTRAP_CYCLES from ALL metric math.
    post_boot = v3[BOOTSTRAP_CYCLES:] if cycles_total > BOOTSTRAP_CYCLES else []
    cycles_post = len(post_boot)

    # Full-run figures (for the capture_ratio denominator, reward totals)
    total_reward_full = sum(x["reward"] for x in v3)

    budget = hist[0].total_capital_budget if hist else 0.0

    if cycles_post == 0:
        return {
            "scenario": r.scenario,
            "cycles_total": cycles_total,
            "post_boot": 0,
            "budget": budget,
            "total_reward_full_run": total_reward_full,
            "active_cycles": 0,
        }

    # V3.1 PART 2 — ACTIVE-only slice (from post-bootstrap set).
    active = [x for x in post_boot if x.get("learning_mode") == "ACTIVE"]
    n_active = len(active)

    # Expected capital evolution (PART 3)
    exp_series = [x["expected_capital"] for x in post_boot]
    exp_max = max(exp_series) if exp_series else 0.0
    exp_var = _variance(exp_series)
    exp_avg = _mean(exp_series) or 0.0
    exp_active_avg = _mean([x["expected_capital"] for x in active]) or 0.0

    # Overcommit (ACTIVE-only average per V3.1 PART 2)
    overcommit_active_series = [x["overcommit_ratio"] for x in active]
    avg_overcommit_active = _mean(overcommit_active_series) or 0.0
    overcommit_max = max(
        (x["overcommit_ratio"] for x in post_boot), default=0.0,
    )

    # Efficiency — V3.1 PART 4 — front vs back mean on post-bootstrap rped.
    rped_series = [
        x["reward_per_expected_dollar"] for x in post_boot
        if x["reward_per_expected_dollar"] is not None
    ]
    if len(rped_series) >= 4:
        mid = len(rped_series) // 2
        eff_front = _mean(rped_series[:mid])
        eff_back = _mean(rped_series[mid:])
    else:
        eff_front = None
        eff_back = None

    rped_active = [
        x["reward_per_expected_dollar"] for x in active
        if x["reward_per_expected_dollar"] is not None
    ]
    rped_active_mean = _mean(rped_active)

    # Exploration — V3.1 PART 6 — % of deployable (not total_capital).
    exploration_ratios = []
    for x in post_boot:
        depl = x.get("deployable_capital") or 0.0
        expl = x.get("exploration_capital") or 0.0
        if depl > 0:
            exploration_ratios.append(expl / depl)
        else:
            exploration_ratios.append(0.0)
    exploration_max_of_deployable = (
        max(exploration_ratios) if exploration_ratios else 0.0
    )

    # Tail risk (V3 INV2 — unchanged)
    tail_series = [x["tail_risk_proxy"] for x in post_boot]
    tail_max = max(tail_series) if tail_series else 0.0

    # Capital scale trajectory (post-bootstrap) for oscillation + clamps.
    cap_scales = [x["capital_scale"] for x in post_boot]
    cap_start = cap_scales[0]
    cap_end = cap_scales[-1]
    cap_min = min(cap_scales) if cap_scales else 1.0
    cap_max = max(cap_scales) if cap_scales else 1.0
    osc_windows = _sign_change_windows(cap_scales)

    # Post-bootstrap reward + capture ratio (using FULL-run reward so the
    # theoretical ceiling is comparable)
    reward_post = sum(x["reward"] for x in post_boot)
    max_reward_full = _theoretical_max_reward(r.scenario, cycles_total)
    capture = total_reward_full / max_reward_full if max_reward_full > 0 else 0.0

    return {
        "scenario": r.scenario,
        "cycles_total": cycles_total,
        "post_boot": cycles_post,
        "active_cycles": n_active,
        "budget": budget,
        # Expected capital / utilisation (V3.1 PART 3)
        "expected_capital_max": exp_max,
        "expected_capital_variance": exp_var,
        "expected_capital_avg": exp_avg,
        "expected_capital_active_avg": exp_active_avg,
        # Overcommit (V3.1 PART 2)
        "avg_overcommit_active": avg_overcommit_active,
        "overcommit_max": overcommit_max,
        # Efficiency (V3.1 PART 4)
        "eff_front": eff_front,
        "eff_back": eff_back,
        "rped_active_mean": rped_active_mean,
        # Tail risk (unchanged)
        "tail_risk_max": tail_max,
        # Exploration (V3.1 PART 6)
        "exploration_max_of_deployable": exploration_max_of_deployable,
        # Reward & capture
        "total_reward_full_run": total_reward_full,
        "reward_post_bootstrap": reward_post,
        "max_reward_theoretical": max_reward_full,
        "capture_ratio": capture,
        # Capital scale
        "capital_scale_start": cap_start,
        "capital_scale_end": cap_end,
        "capital_scale_min": cap_min,
        "capital_scale_max": cap_max,
        "capital_scale_delta": cap_end - cap_start,
        "oscillation_windows": osc_windows,
    }


def _evaluate_seed_v31(results: list[SimulationResult]) -> dict:
    """Apply the V3.1 invariants + criteria to one seed's scenarios."""
    by_scen = {r.scenario: _per_scenario_v31_metrics(r) for r in results}
    failures: list[str] = []
    per_scenario_results: dict = {
        s: {"result": "PASS", "reasons": []} for s in by_scen
    }

    def _fail(scenario: str, code: str, detail: str) -> None:
        failures.append(f"[{scenario}] {code}: {detail}")
        bucket = per_scenario_results.setdefault(
            scenario, {"result": "PASS", "reasons": []},
        )
        bucket["result"] = "FAIL"
        bucket["reasons"].append(f"{code}: {detail}")

    # ── V3.1 INVARIANTS ─────────────────────────────────────

    for scen, m in by_scen.items():
        if m.get("cycles_total", 0) == 0:
            _fail(scen, "NO_DATA", "scenario produced zero cycles")
            continue

        if m.get("post_boot", 0) == 0:
            _fail(
                scen, "BOOTSTRAP_TRUNCATED",
                f"run length {m['cycles_total']} ≤ BOOTSTRAP_CYCLES="
                f"{BOOTSTRAP_CYCLES}; no post-bootstrap data",
            )
            continue

        budget = m.get("budget") or 0.0

        # INV1 (V3-preserved) — expected_capital ≤ total_capital
        if budget > 0 and m["expected_capital_max"] > budget * EXP_CAP_HARD_CAP:
            _fail(
                scen, "INV1_expected_capital_exceeded",
                f"max expected_capital=${m['expected_capital_max']:.2f} > "
                f"budget × {EXP_CAP_HARD_CAP} (${budget:.0f})",
            )

        # INV2 (V3-preserved) — tail_risk_proxy ≤ 0.30
        if m["tail_risk_max"] > TAIL_RISK_MAX + 1e-6:
            _fail(
                scen, "INV2_tail_risk_exceeded",
                f"max tail_risk={m['tail_risk_max']:.2%} > "
                f"{TAIL_RISK_MAX:.0%}",
            )

        # INV3 (V3.1 PART 2) — ACTIVE-only overcommit avg with guard
        n_active = m.get("active_cycles", 0)
        if n_active < ACTIVE_MIN_CYCLES:
            _fail(
                scen, "INV3_insufficient_active_cycles",
                f"only {n_active} ACTIVE cycles post-bootstrap "
                f"(need ≥{ACTIVE_MIN_CYCLES})",
            )
        else:
            if m["avg_overcommit_active"] < OVERCOMMIT_MIN_ACTIVE:
                _fail(
                    scen, "INV3_overcommit_active_below_min",
                    f"avg overcommit (ACTIVE-only) = "
                    f"{m['avg_overcommit_active']:.2f} < "
                    f"{OVERCOMMIT_MIN_ACTIVE}",
                )

        # INV4 (V3.1 PART 3) — expected_capital > 0 AND evolves
        if m["expected_capital_max"] <= 0:
            _fail(
                scen, "INV4_expected_capital_never_positive",
                "expected_capital is zero across the entire post-bootstrap "
                "window",
            )
        elif m["expected_capital_variance"] <= 0:
            _fail(
                scen, "INV4_expected_capital_not_evolving",
                "expected_capital variance = 0 (constant over run)",
            )

        # INV5 (V3.1 PART 4) — eff_back ≥ 0.70 × eff_front
        ef, eb = m.get("eff_front"), m.get("eff_back")
        if ef is None or eb is None:
            _fail(
                scen, "INV5_efficiency_retention_no_data",
                "insufficient post-bootstrap reward_per_expected_dollar "
                "samples",
            )
        elif ef > 0 and eb < EFF_RETENTION_FLOOR * ef:
            _fail(
                scen, "INV5_efficiency_retention_below_floor",
                f"eff_back={eb:.4f} < {EFF_RETENTION_FLOOR} × "
                f"eff_front={ef:.4f} ({EFF_RETENTION_FLOOR * ef:.4f})",
            )

        # INV6 (V3.1 PART 6) — exploration ≤ 15% of deployable
        if m["exploration_max_of_deployable"] > EXPLORATION_MAX_FRAC_DEPL + 1e-6:
            _fail(
                scen, "INV6_exploration_exceeded_deployable",
                f"max exploration_frac={m['exploration_max_of_deployable']:.2%} "
                f"> {EXPLORATION_MAX_FRAC_DEPL:.0%} of deployable",
            )

        # INV7 (unchanged — persistent oscillation is a real signal)
        lo, hi = CLAMP_CAP
        if m["capital_scale_min"] < lo - 1e-9:
            _fail(
                scen, "INV7_capital_scale_below_clamp",
                f"min capital_scale={m['capital_scale_min']:.3f} < {lo}",
            )
        if m["capital_scale_max"] > hi + 1e-9:
            _fail(
                scen, "INV7_capital_scale_above_clamp",
                f"max capital_scale={m['capital_scale_max']:.3f} > {hi}",
            )
        if m["oscillation_windows"] > OSC_WIN_LIMIT:
            _fail(
                scen, "INV7_persistent_oscillation",
                f"{m['oscillation_windows']} consecutive {OSC_WIN}-cycle "
                f"windows with ≥{OSC_SIGN_FLIPS} flips",
            )

    # ── V3.1 CROSS-SCENARIO CRITERIA ─────────────────────────

    so = by_scen.get("stable_optimal", {})
    oa = by_scen.get("over_aggressive", {})

    # Criterion C (V3.1 PART 5) — dominance by TOTAL REWARD, not rped
    so_rew = so.get("total_reward_full_run")
    oa_rew = oa.get("total_reward_full_run")
    if so_rew is None or oa_rew is None:
        _fail("stable_optimal", "C_dominance_no_data",
              f"so_rew={so_rew}, oa_rew={oa_rew}")
    elif so_rew <= oa_rew:
        _fail(
            "stable_optimal", "C_not_dominating_over_aggressive",
            f"total_reward(stable)=${so_rew:.2f} !> "
            f"total_reward(over_aggr)=${oa_rew:.2f}",
        )

    # Criterion D — under_deployed max cap_scale ≥ 1.10
    ud = by_scen.get("under_deployed", {})
    ud_max = ud.get("capital_scale_max", 0.0)
    if ud_max < UNDER_DEPLOY_CAP_MIN:
        _fail(
            "under_deployed", "D_expansion_missed",
            f"max(capital_scale)={ud_max:.3f} < {UNDER_DEPLOY_CAP_MIN}",
        )

    # Criterion E — over_aggressive final < initial
    oa_start = oa.get("capital_scale_start", 1.0)
    oa_end = oa.get("capital_scale_end", 1.0)
    if oa_end >= oa_start:
        _fail(
            "over_aggressive", "E_contraction_missed",
            f"final cap={oa_end:.3f} !< initial cap={oa_start:.3f}",
        )

    # Criterion F — regime_shift frontier_memory ≥ 2
    rs_result = next(
        (r for r in results if r.scenario == "regime_shift"), None,
    )
    if rs_result is not None and rs_result.learning_state_history:
        mem_size = int(
            rs_result.learning_state_history[-1].get(
                "frontier_memory_size",
            ) or 0
        )
        if mem_size < 2:
            _fail(
                "regime_shift", "F_regime_adaptation_missed",
                f"frontier_memory_size end={mem_size} < 2",
            )

    # Criterion G — stable_optimal capture ≥ 70%
    so_capture = so.get("capture_ratio", 0.0)
    if so_capture < CAPTURE_RATIO_MIN:
        _fail(
            "stable_optimal", "G_capture_ratio_below_min",
            f"capture_ratio={so_capture:.2%} < {CAPTURE_RATIO_MIN:.0%}",
        )

    status = "PASS" if not failures else "FAIL"
    return {
        "status": status,
        "per_scenario": by_scen,
        "per_scenario_results": per_scenario_results,
        "failures": failures,
    }


def build_report_v3(results_by_seed: dict) -> dict:
    """V3.1 report builder. Function name preserved so run_audit_v3 keeps
    working unchanged."""
    per_seed_reports: dict = {}
    all_failures: list[str] = []
    for seed in sorted(results_by_seed.keys()):
        seed_report = _evaluate_seed_v31(results_by_seed[seed])
        per_seed_reports[seed] = seed_report
        if seed_report["status"] == "FAIL":
            for f in seed_report["failures"]:
                all_failures.append(f"seed={seed} | {f}")

    multi_seed_pass = all(
        r["status"] == "PASS" for r in per_seed_reports.values()
    )
    status = "PASS" if multi_seed_pass else "FAIL"

    # Global totals (full-run)
    total_reward = 0.0
    total_loss = sum(
        r.cumulative_loss
        for seed_results in results_by_seed.values()
        for r in seed_results
    )
    avg_overcommit_active_all = []
    avg_rped_active_all = []
    avg_expected_active_all = []
    tail_maxs = []
    exploration_max_of_deployable_all = []
    for seed_report in per_seed_reports.values():
        for m in seed_report["per_scenario"].values():
            total_reward += m.get("total_reward_full_run", 0.0)
            if m.get("avg_overcommit_active"):
                avg_overcommit_active_all.append(m["avg_overcommit_active"])
            if m.get("rped_active_mean") is not None:
                avg_rped_active_all.append(m["rped_active_mean"])
            if m.get("expected_capital_active_avg"):
                avg_expected_active_all.append(m["expected_capital_active_avg"])
            tail_maxs.append(m.get("tail_risk_max", 0.0))
            exploration_max_of_deployable_all.append(
                m.get("exploration_max_of_deployable", 0.0)
            )

    return {
        "status": status,
        "version": "3.1",
        "bootstrap_cycles_excluded": BOOTSTRAP_CYCLES,
        "per_seed": per_seed_reports,
        "multi_seed_pass": multi_seed_pass,
        "failures": all_failures,
        "global_totals": {
            "total_reward": total_reward,
            "total_loss": total_loss,
            "avg_overcommit_active": (
                sum(avg_overcommit_active_all) / len(avg_overcommit_active_all)
                if avg_overcommit_active_all else 0.0
            ),
            "avg_expected_capital_active": (
                sum(avg_expected_active_all) / len(avg_expected_active_all)
                if avg_expected_active_all else 0.0
            ),
            "avg_rped_active": (
                sum(avg_rped_active_all) / len(avg_rped_active_all)
                if avg_rped_active_all else 0.0
            ),
            "max_tail_risk": max(tail_maxs) if tail_maxs else 0.0,
            "max_exploration_of_deployable": (
                max(exploration_max_of_deployable_all)
                if exploration_max_of_deployable_all else 0.0
            ),
        },
    }


def print_report_v3(report: dict) -> None:
    print()
    print("=" * 60)
    print("=== FINAL SYSTEM AUDIT V3.1 ===")
    print("=" * 60)
    print()
    print(f"STATUS: {report.get('status', 'UNKNOWN')}")
    print()
    print(
        f"Note: metrics computed excluding the first "
        f"{report.get('bootstrap_cycles_excluded', BOOTSTRAP_CYCLES)} "
        f"bootstrap cycles."
    )
    print()

    for seed in sorted(report.get("per_seed", {}).keys()):
        sr = report["per_seed"][seed]
        print(f"--- seed={seed} --- status={sr['status']}")
        print()
        for scen in (
            "stable_optimal", "under_deployed", "over_aggressive",
            "high_reward_fake", "regime_shift",
        ):
            m = sr["per_scenario"].get(scen)
            res = sr["per_scenario_results"].get(
                scen, {"result": "N/A", "reasons": []}
            )
            if not m:
                print(f"  {scen}: (no data)   RESULT: N/A")
                continue
            print(f"  {scen}:")
            print(
                f"    cycles (total / post-boot / ACTIVE)  : "
                f"{m.get('cycles_total', 0)} / "
                f"{m.get('post_boot', 0)} / "
                f"{m.get('active_cycles', 0)}"
            )
            print(
                f"    expected_capital (avg/max)            : "
                f"${m.get('expected_capital_avg', 0):.2f} / "
                f"${m.get('expected_capital_max', 0):.2f}  "
                f"(var={m.get('expected_capital_variance', 0):.4f})"
            )
            print(
                f"    avg_overcommit_active                 : "
                f"{m.get('avg_overcommit_active', 0):.3f}"
            )
            print(
                f"    overcommit_max (post-boot)            : "
                f"{m.get('overcommit_max', 0):.3f}"
            )
            print(
                f"    tail_risk_max                         : "
                f"{m.get('tail_risk_max', 0):.2%}"
            )
            print(
                f"    exploration_max_frac_deployable       : "
                f"{m.get('exploration_max_of_deployable', 0):.2%}"
            )
            print(
                f"    total_reward (full run)               : "
                f"${m.get('total_reward_full_run', 0):.2f}"
            )
            ef = m.get("eff_front")
            eb = m.get("eff_back")
            ef_s = f"{ef:.4f}" if ef is not None else "n/a"
            eb_s = f"{eb:.4f}" if eb is not None else "n/a"
            print(
                f"    efficiency (front / back)             : "
                f"{ef_s} / {eb_s}"
            )
            print(
                f"    capital_scale trajectory              : "
                f"{m.get('capital_scale_start', 1.0):.3f} → "
                f"{m.get('capital_scale_end', 1.0):.3f} "
                f"(range [{m.get('capital_scale_min', 1.0):.3f}, "
                f"{m.get('capital_scale_max', 1.0):.3f}])"
            )
            print(
                f"    capture_ratio                         : "
                f"{m.get('capture_ratio', 0):.2%}"
            )
            print(f"    RESULT: {res['result']}")
            for reason in res["reasons"]:
                print(f"      - {reason}")
            print()

    g = report.get("global_totals", {})
    print("=" * 60)
    print("Global (ACTIVE-only where applicable):")
    print(f"  total_reward                    : ${g.get('total_reward', 0):.2f}")
    print(f"  total_loss                      : ${g.get('total_loss', 0):.2f}")
    print(
        f"  avg_overcommit_active           : "
        f"{g.get('avg_overcommit_active', 0):.3f}"
    )
    print(
        f"  avg_expected_capital_active     : "
        f"${g.get('avg_expected_capital_active', 0):.3f}"
    )
    print(
        f"  avg_rped_active                 : "
        f"{g.get('avg_rped_active', 0):.3f}"
    )
    print(
        f"  max_tail_risk                   : "
        f"{g.get('max_tail_risk', 0):.2%}"
    )
    print(
        f"  max_exploration_of_deployable   : "
        f"{g.get('max_exploration_of_deployable', 0):.2%}"
    )
    print(
        f"  multi_seed_pass                 : "
        f"{'YES' if report.get('multi_seed_pass') else 'NO'}"
    )
    print()
    if report.get("failures"):
        print("Failure list:")
        for f in report["failures"]:
            print(f"  - {f}")
        print()
    print(f"FINAL VERDICT: {report.get('status', 'UNKNOWN')}")
    print("=" * 60)
