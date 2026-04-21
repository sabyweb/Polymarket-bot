"""simulation/v3_metrics.py — V3 audit per-cycle metric computation.

V3 replaces naive deployed_capital with EXPECTED capital usage under
Polymarket's overcommitment model:

    expected_capital = Σ (p_fill_i × order_size_i)

This reflects actual capital-at-risk: orders only consume capital when
they fill, and Polymarket auto-cancels the opposing side of a market
once one side fills. `reward_per_expected_dollar` is the V3 "real
efficiency" metric that the audit's PASS/FAIL criteria key off.

Pure function: reads a cycle outcome + learning state, returns a dict.
No side effects, no I/O.
"""

from __future__ import annotations
from typing import Optional

from profit.learning import LearningState

from .runner import CycleOutcome


def compute_v3_cycle(
    outcome: CycleOutcome,
    total_capital: float,
    applied_state: LearningState,
) -> dict:
    """V3 per-cycle metric record.

    Args:
      outcome        : cycle result (includes allocations with _p_fill,
                       est_capital_cost, _cluster_id, _exploration flags).
      total_capital  : the nominal capital ceiling for the audit run.
      applied_state  : the LearningState actually applied this cycle
                       (from LearningStep.applied_state).

    Returns a dict with:
      - expected_capital, total_notional, overcommit_ratio
      - cluster_max_exposure, tail_risk_proxy
      - exploration_capital
      - reward, reward_per_expected_dollar
      - capital_scale
      - learning_mode (V3.1: gate state that ran this cycle)
      - deployable_capital (V3.1: overcommit-expanded ceiling)
    """
    mode = str(getattr(applied_state, "mode", "OFF") or "OFF")
    deploys = [
        a for a in outcome.allocations if a.get("action") == "deploy"
    ]

    if not deploys:
        return {
            "cycle": outcome.cycle,
            "n_deploys": 0,
            "expected_capital": 0.0,
            "total_notional": 0.0,
            "overcommit_ratio": 0.0,
            "cluster_max_exposure": 0.0,
            "tail_risk_proxy": 0.0,
            "exploration_capital": 0.0,
            "reward": float(outcome.reward),
            "loss": float(outcome.loss),
            "reward_per_expected_dollar": None,
            "capital_scale": float(applied_state.capital_scale),
            "learning_mode": mode,
            "deployable_capital": float(total_capital),
            "overcommit_factor_applied": 1.0,
        }

    # Expected capital (V3 core):
    # Σ (p_fill × est_capital_cost). p_fill is the allocator-stamped
    # per-market 24h fill probability. est_capital_cost is the total
    # notional (both sides) — Polymarket auto-cancels the opposing
    # side on fill, so the multiplicative formulation captures the
    # expected consumption.
    expected_capital = 0.0
    total_notional = 0.0
    cluster_totals: dict = {}
    exploration_cap = 0.0
    for a in deploys:
        p_fill = float(a.get("_p_fill") or 0.0)
        cost = float(a.get("est_capital_cost") or 0.0)
        expected_capital += p_fill * cost
        total_notional += cost
        # Cluster bucketing: prefer _cluster_id when clustering fired,
        # else fall back to condition_id (per-market is its own "cluster").
        cid = a.get("_cluster_id") or a.get("condition_id")
        cluster_totals[cid] = cluster_totals.get(cid, 0.0) + cost
        if a.get("_exploration"):
            exploration_cap += cost

    cluster_max = max(cluster_totals.values()) if cluster_totals else 0.0

    overcommit_ratio = (
        total_notional / total_capital if total_capital > 0 else 0.0
    )
    tail_risk = (
        cluster_max / total_capital if total_capital > 0 else 0.0
    )

    # V3 efficiency = reward per EXPECTED dollar
    rped = (
        outcome.reward / expected_capital if expected_capital > 0 else None
    )

    # V3.1 — deployable capital (overcommit-expanded ceiling used by the
    # allocator this cycle). Read the stamped _overcommit_factor from any
    # deploy row; fall back to 1.0 when missing (legacy / OFF-SHADOW).
    oc_factor = 1.0
    for a in deploys:
        v = a.get("_overcommit_factor")
        if v is not None:
            try:
                oc_factor = float(v)
                break
            except (TypeError, ValueError):
                continue
    deployable_capital = float(total_capital) * oc_factor

    return {
        "cycle": outcome.cycle,
        "n_deploys": len(deploys),
        "expected_capital": expected_capital,
        "total_notional": total_notional,
        "overcommit_ratio": overcommit_ratio,
        "cluster_max_exposure": cluster_max,
        "tail_risk_proxy": tail_risk,
        "exploration_capital": exploration_cap,
        "reward": float(outcome.reward),
        "loss": float(outcome.loss),
        "reward_per_expected_dollar": rped,
        "capital_scale": float(applied_state.capital_scale),
        "learning_mode": mode,
        "deployable_capital": deployable_capital,
        "overcommit_factor_applied": oc_factor,
    }
