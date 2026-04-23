"""Continuous allocator — reward/loss-weighted capital distribution.

Replaces the Patch 6/7/9/10/11/13 stack (overcommit factor, target-driven
greedy fill, forced-exposure promotion, marginal-efficiency gate,
oscillation damping, hysteresis, etc.). One continuous formula turns
(R, p, L, cpb, total_capital, λ1, λ2) into per-market shares, with hard
safety caps (per-market / per-cluster / per-question-group) applied as
post-clip only — no redistribution, no ranking distortion.

Pipeline (strict order — do not reorder):

    1. compute weights      w_i = R_i / (λ1 · p_i · L_i + λ2)
    2. raw allocation       raw_i = w_i²
    3. scale to capital     C_i = raw_i · (0.95 · total_capital / Σ p_i·raw_i)
    4. convert to shares    shares_i = max(min_size, int(C_i / cpb_i))
                            with floor C_i ≥ cpb_i · min_shares
    5. apply caps           per-market, per-question-group, per-cluster
    6. recompute            expected_capital = Σ p_i · est_capital_cost_i
    7. final rescale        if > 0.95 · total_capital, scale uniformly down

Inputs per market (§2 of spec):

    R_i    = predictions.raw_reward_per_day    (clean reward, not EV-derived)
    p_i    = max(1e-4, predictions.p_fill_24h)
    L_i    = predictions.e_loss_given_fill
    cpb_i  = 2 · max(0.10, (1 - 2·spread) / 2)

Globals:

    total_capital   — caller input (caller applies capital_scale etc.)
    λ1              — LearningState.lambda_1, bounds [0.5, 5.0]
    λ2              — LearningState.lambda_2, bounds [0.01, 2.0]

Guarantees:

    - Never returns zero deployments while any deploy candidate exists.
    - No binary filtering — continuous weights only.
    - Smooth in input perturbations.
    - Deterministic for fixed inputs.
    - learning_state=None is a valid call (uses default λ1, λ2).

The SafetyController still runs downstream via allocation_writer; this
module does not bypass or duplicate it.
"""

import logging

from oversight.market_scorer import ScoredMarket
from oversight.allocation_writer import _to_dict
from .correlation import (
    build_fill_clusters, apply_cluster_caps, DEFAULT_MAX_CLUSTER_PCT,
    OVERSIZED_CLUSTER_PCT,
)

log = logging.getLogger("profit.allocator")

# Capital safety parameters ─────────────────────────────────────
CAPITAL_BUFFER = 0.95              # keep 5% headroom below total_capital
WEIGHT_FLOOR = 1e-6                # never-zero weight
SCALE_EPSILON = 1e-9               # degenerate expected_total → equal fallback

# Learning-loop default control values — must match LearningState defaults.
# β = utilisation target (replaces the hardcoded 0.95 scaling constant
# in Step 3). η = concentration exponent (replaces the w² squaring).
DEFAULT_BETA = 0.75                # bounds [0.1, 0.95]
DEFAULT_ETA  = 0.0                 # bounds [0.0, 4.0] — raw_i = w_i^(1+η)


def _cost_per_share(spread: float) -> float:
    """Round-trip cost per share for placing both sides at a given spread.
    Matches the formula used across the codebase (see allocation_writer's
    _est_market_cost): 2 · max(0.10, (1 - 2·spread) / 2)."""
    s = spread if spread > 0 else 0.045
    return 2.0 * max(0.10, (1.0 - 2.0 * s) / 2.0)


def _recompute_stamps(a: dict) -> None:
    """Refresh _expected_capital / _expected_capital_contribution after
    shares_per_side or est_capital_cost is modified by a cap/rescale."""
    p = float(a.get("_p_fill") or 0.0)
    cost = float(a.get("est_capital_cost") or 0.0)
    a["_expected_capital"] = round(p * cost, 4)
    a["_expected_capital_contribution"] = a["_expected_capital"]


def _clip_per_market(allocations: list[dict], cap: float) -> None:
    """Clip any allocation whose cost exceeds the per-market cap.
    Hard safety — no redistribution of freed capital."""
    for a in allocations:
        if a.get("action") != "deploy":
            continue
        cost = float(a.get("est_capital_cost") or 0.0)
        if cost <= cap:
            continue
        shares = int(a.get("shares_per_side") or 0)
        if shares <= 0:
            continue
        cpb = cost / shares
        if cpb <= 0:
            continue
        min_shares = int(a.get("min_size") or 1)
        new_shares = max(min_shares, int(cap / cpb))
        a["shares_per_side"] = new_shares
        a["est_capital_cost"] = round(new_shares * cpb, 2)
        _recompute_stamps(a)


def _clip_per_group(allocations: list[dict], cap: float) -> None:
    """Clip per-question-group exposure. Iterates in input order (which
    is the original scored_markets order) so larger-weight markets get
    funded first and later markets in the same group get clipped."""
    used: dict[str, float] = {}
    for a in allocations:
        if a.get("action") != "deploy":
            continue
        group = str(a.get("question_group") or "")
        if not group:
            continue
        cost = float(a.get("est_capital_cost") or 0.0)
        current = used.get(group, 0.0)
        if current + cost <= cap:
            used[group] = current + cost
            continue
        headroom = max(0.0, cap - current)
        shares = int(a.get("shares_per_side") or 0)
        if shares <= 0:
            continue
        cpb = cost / shares
        if cpb <= 0:
            continue
        min_shares = int(a.get("min_size") or 1)
        new_shares = max(min_shares, int(headroom / cpb)) if headroom > 0 else min_shares
        a["shares_per_side"] = new_shares
        a["est_capital_cost"] = round(new_shares * cpb, 2)
        _recompute_stamps(a)
        used[group] = current + float(a["est_capital_cost"])


def _apply_cluster_cap_safe(
    allocations: list[dict], db_path: str,
    max_cluster_pct: float, total_capital: float,
) -> list[dict]:
    """Apply per-cluster cap via the existing correlation module.
    Fail-open: if clustering can't be computed, we still produce an
    allocation. Caps are safety-only, not optimisation logic."""
    try:
        clusters, oversized = build_fill_clusters(db_path)
    except Exception as e:
        log.warning(f"[ALLOC] cluster build skipped: {e}")
        return allocations
    if not clusters:
        return allocations
    try:
        allocations = apply_cluster_caps(
            allocations, clusters, max_cluster_pct, total_capital, oversized,
        )
    except Exception as e:
        log.warning(f"[ALLOC] apply_cluster_caps failed (skipped): {e}")
        return allocations
    # apply_cluster_caps mutates shares/cost — refresh the expected-capital
    # stamps so step 6 sees consistent data.
    for a in allocations:
        if a.get("action") == "deploy":
            _recompute_stamps(a)
    return allocations


def allocate_portfolio(
    scored_markets: list[ScoredMarket],
    total_capital: float,
    calibrator,  # CalibrationManager
    db_path: str = "bot_history.db",
    max_per_market: float = 200.0,
    max_capital_pct: float = 0.15,
    max_group_pct: float = 0.30,
    max_cluster_pct: float = DEFAULT_MAX_CLUSTER_PCT,
    learning_state=None,  # profit.learning.LearningState | None
) -> list[dict]:
    """Continuous weight-based capital allocation.

    Returns a list[dict] in the established JSON schema. Markets the
    scorer already decided to avoid (sports protection, trial cap,
    explicit deploy=False) are passed through as zero-share avoid rows.
    """
    if not scored_markets:
        return []
    if total_capital <= 0:
        # No capital → pass-through avoids only. Allocation shape stays
        # consistent for the farmer and safety controller.
        return [_to_dict(sm, shares=0) for sm in scored_markets]

    beta = DEFAULT_BETA
    eta  = DEFAULT_ETA
    if learning_state is not None:
        beta = float(getattr(learning_state, "beta", DEFAULT_BETA))
        eta  = float(getattr(learning_state, "eta",  DEFAULT_ETA))

    # Step 0: split scored markets into deploy candidates + pass-through
    # avoids. Preserves upstream decisions (sports block, trial cap).
    deploy_candidates: list[dict] = []
    passthrough_avoids: list[dict] = []
    for sm in scored_markets:
        if sm.action != "deploy":
            passthrough_avoids.append(_to_dict(sm, shares=0))
            continue
        try:
            preds = calibrator.get_predictions(
                condition_id=sm.condition_id,
                daily_rate=sm.daily_rate,
                q_share_pct=sm.q_share_pct,
                on_book_hours=0,
                fill_count_recent=sm.fill_count,
                fill_cost_recent=sm.fill_damage,
                dump_revenue_recent=0,
                agent_shares=sm.min_size,
                correction_factor=1.0,
            )
        except Exception as e:
            log.warning(
                f"[ALLOC] get_predictions failed for {sm.condition_id}: {e}"
            )
            preds = None
        if preds is None:
            row = _to_dict(sm, shares=0)
            row["reason"] = "no calibration prediction"
            passthrough_avoids.append(row)
            continue

        R = float(getattr(preds, "raw_reward_per_day", 0.0) or 0.0)
        p = max(1e-4, float(preds.p_fill_24h or 0.0))
        L = max(0.0, float(preds.e_loss_given_fill or 0.0))
        spread = float(getattr(sm, "max_spread", 0.045) or 0.045)
        cpb = _cost_per_share(spread)

        deploy_candidates.append({
            "sm": sm, "R": R, "p": p, "L": L, "cpb": cpb, "preds": preds,
        })

    if not deploy_candidates:
        return passthrough_avoids

    # ── Step 1: weights ────────────────────────────────────────
    # w_i = R_i / (1 + p_i · L_i). Denominator has no control variable;
    # concentration is expressed via η in Step 2, not in the ranking.
    # E_loss stamped on the candidate dict so we can emit _expected_loss
    # without recomputing later.
    for d in deploy_candidates:
        E_loss = d["p"] * d["L"]
        denom = 1.0 + E_loss
        d["E_loss"] = E_loss
        d["weight"] = max(WEIGHT_FLOOR, d["R"] / denom)

    # ── Step 2: raw allocation ─────────────────────────────────
    # raw_i = w_i^(1 + η). η is the concentration control (spec §3.2):
    # η = 0 reproduces a linear weighting; higher η concentrates capital
    # on the top-weight markets.
    concentration = 1.0 + eta
    for d in deploy_candidates:
        d["raw_alloc"] = d["weight"] ** concentration

    # ── Step 3: scale so expected capital consumption ≈ β · total ──
    # β is the utilisation target control (spec §3.3). It replaces the
    # previously hardcoded 0.95 scaling constant in this step. The hard
    # 0.95 safety ceiling is retained in Step 7 below (unchanged).
    expected_total = sum(d["p"] * d["raw_alloc"] for d in deploy_candidates)
    budget = total_capital * beta

    if expected_total < SCALE_EPSILON:
        # Every weight collapsed — fall back to equal allocation so the
        # system continues to deploy. Rare; indicates R ≈ 0 everywhere
        # across all deploy candidates.
        equal = budget / len(deploy_candidates)
        for d in deploy_candidates:
            d["C"] = equal
        log.info(
            f"[ALLOC] expected_total degenerate ({expected_total:.3e}); "
            f"equal allocation across {len(deploy_candidates)} markets"
        )
    else:
        scale = budget / expected_total
        for d in deploy_candidates:
            d["C"] = d["raw_alloc"] * scale

    # ── Step 3b: cap-aware shaping (binding-cluster subset selection) ──
    # Under the cluster-cap × min-floor composition, when a cluster's
    # proportional per-member budget falls below a member's min_capital
    # floor, Step 5 cluster cap + Step 4 min-shares floor compose to pin
    # every member to min_capital — erasing the β/η signal. Pre-select a
    # top-k subset per binding cluster so the cap stack receives a sized-
    # to-fit set and allocator expressivity is preserved. Non-selected
    # members become zero-share avoid rows (§4.3 guarantee 3). Non-binding
    # clusters and unclustered markets are untouched (§4.4 guarantee 4).
    try:
        _sh_clusters, _sh_oversized = build_fill_clusters(db_path)
    except Exception as e:
        log.warning(f"[ALLOC] cluster build (shaping) skipped: {e}")
        _sh_clusters, _sh_oversized = {}, set()

    if _sh_clusters:
        members_by_cluster: dict[int, list[dict]] = {}
        for d in deploy_candidates:
            cl_id = _sh_clusters.get(d["sm"].condition_id)
            if cl_id is None:
                continue
            members_by_cluster.setdefault(cl_id, []).append(d)

        for cl_id, members in members_by_cluster.items():
            if not members:
                continue  # empty cluster → skip (§7 edge case)
            size = len(members)
            cap_pct = (
                OVERSIZED_CLUSTER_PCT if cl_id in _sh_oversized
                else max_cluster_pct
            )
            cluster_budget = cap_pct * total_capital
            cluster_per_market = cluster_budget / size
            mins: list[float] = []
            for d in members:
                msize = int(d["sm"].min_size) if getattr(d["sm"], "min_size", 0) else 1
                if msize <= 0:
                    msize = 1
                mins.append(d["cpb"] * msize)
            cluster_min_capital = max(mins) if mins else 0.0
            if cluster_min_capital <= 0:
                continue
            if cluster_per_market >= cluster_min_capital:
                continue  # non-binding — do nothing (§4.4)
            # Binding: select top-k survivors, zero the rest.
            k = int(cluster_budget // cluster_min_capital)
            if k < 1:
                k = 1  # §7 edge case: cluster_budget < min_capital still k=1
            ordered = sorted(
                members,
                key=lambda d: (-d["raw_alloc"], str(d["sm"].condition_id)),
            )
            for d in ordered[k:]:
                d["C"] = 0.0
                d["_shape_exclude"] = True

        excluded = [d for d in deploy_candidates if d.get("_shape_exclude")]
        if excluded:
            deploy_candidates = [
                d for d in deploy_candidates if not d.get("_shape_exclude")
            ]
            for d in excluded:
                passthrough_avoids.append(_to_dict(
                    d["sm"], shares=0,
                    action_override="avoid",
                    reason_override="cluster shaping deselected",
                ))

    # ── Step 4: convert to shares, enforcing min-capital floor ─
    allocations: list[dict] = []
    for d in deploy_candidates:
        sm = d["sm"]
        cpb = d["cpb"]
        min_shares = int(sm.min_size) if getattr(sm, "min_size", 0) else 1
        if min_shares <= 0:
            min_shares = 1
        min_capital = cpb * min_shares
        # §13A: guarantee deployment — C_i never below min_capital floor.
        capital = max(d["C"], min_capital)
        shares = max(min_shares, int(capital / cpb)) if cpb > 0 else min_shares
        est_cost = round(shares * cpb, 2)

        row = _to_dict(sm, shares=shares)
        row["est_capital_cost"] = est_cost
        # Observability stamps specified in §11 + additional decision #6.
        # Pure observability — none of these are read back by the allocator
        # itself; consumers are the audit framework and the learning-loop
        # metrics engine (which needs total_capital for expected_util).
        row["_p_fill"] = round(d["p"], 6)
        row["_reward"] = round(d["R"], 6)
        row["_expected_loss"] = round(d["E_loss"], 6)
        row["_weight"] = round(d["weight"], 6)
        row["_raw_alloc"] = round(d["raw_alloc"], 6)
        row["_beta"] = round(beta, 6)
        row["_eta"]  = round(eta,  6)
        row["_total_capital"] = round(total_capital, 2)
        _recompute_stamps(row)
        allocations.append(row)

    # ── Step 5: caps (hard safety, clip-only) ─────────────────
    per_market_cap = min(max_per_market, total_capital * max_capital_pct)
    per_group_cap = total_capital * max_group_pct
    _clip_per_market(allocations, per_market_cap)
    _clip_per_group(allocations, per_group_cap)
    allocations = _apply_cluster_cap_safe(
        allocations, db_path, max_cluster_pct, total_capital,
    )

    # ── Step 6: recompute expected_capital after caps ──────────
    expected_capital = sum(
        float(a.get("_p_fill") or 0.0)
        * float(a.get("est_capital_cost") or 0.0)
        for a in allocations
        if a.get("action") == "deploy"
    )

    # ── Step 7: final rescale if over the 95% ceiling ──────────
    ceiling = total_capital * CAPITAL_BUFFER
    if expected_capital > ceiling and expected_capital > 0:
        rescale = ceiling / expected_capital
        for a in allocations:
            if a.get("action") != "deploy":
                continue
            cost_old = float(a["est_capital_cost"])
            shares_old = int(a["shares_per_side"] or 0)
            if shares_old <= 0:
                continue
            cpb = cost_old / shares_old
            if cpb <= 0:
                continue
            new_cost = cost_old * rescale
            min_shares = int(a.get("min_size") or 1)
            shares = max(min_shares, int(new_cost / cpb))
            a["shares_per_side"] = shares
            a["est_capital_cost"] = round(shares * cpb, 2)
            _recompute_stamps(a)
        log.info(
            f"[ALLOC] final rescale: expected ${expected_capital:.2f} > "
            f"ceiling ${ceiling:.2f}; scale={rescale:.3f}"
        )

    deployed = [a for a in allocations if a.get("action") == "deploy"]
    total_cost = sum(float(a.get("est_capital_cost") or 0.0) for a in deployed)
    total_expected = sum(
        float(a.get("_expected_capital") or 0.0) for a in deployed
    )
    log.info(
        f"[ALLOC] β={beta:.3f} η={eta:.3f} | "
        f"{len(deployed)} deploy + {len(passthrough_avoids)} avoid | "
        f"notional ${total_cost:.0f} / expected ${total_expected:.0f} / "
        f"cap ${total_capital:.0f}"
    )

    return allocations + passthrough_avoids
