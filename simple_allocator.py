"""SimpleAllocator — Path B-prime replacement for the multi-layer
SafetyController + LearningController + calibration + β/η stack.

Phase 0 + 7-agent investigation findings:
- The existing ~10,000 LOC of safety/learning machinery is dormant at $226 wallet
  (every learning component produces no signal that flows to bot decisions).
- Polymarket's `/rewards/user/percentages` exposes the real q_share Polymarket
  itself measures (in percent units; divide by 100 for fraction).
- The bot's local q_share estimators (Priority 1 windowed × 0.5 cap; Priority 2
  cumulative DB ratio) over-estimate by 500× and under-estimate by ~2× respectively.
- $1/day per-user payout threshold means below-threshold accruals are lost.

This module replaces estimation+state-machine+learning with:
  Priority 0:  Polymarket API for currently-positioned markets (real, percent units)
  Priority 1:  Cumulative reward_market_stats ratio for previously-seen markets
  Priority 2:  Cold-start prior for unseen markets

Output schema matches `oversight/allocation_writer.compute_allocations` so the
farmer's existing `market_allocations.json` reader is unchanged.

This is a NEW module. It is NOT YET WIRED into the oversight loop. To activate,
swap the systemd ExecStart from `oversight_agent.py` to a new `simple_oversight.py`
entry point (separate commit).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from config import cfg

log = logging.getLogger("simple_allocator")


# ── Configuration ──
#
# P2 of 9/10 plan (FX-052 + FX-053): transform from SimpleAllocator's
# capped/budget-bound semantics into the OverCommitAllocator per ground
# rules 1 + 2. Old behavior capped total notional below wallet (Rule 2
# violation) and capped market count at 20 (Rule 1 violation: 5000+
# reward markets ignored). New behavior:
#   - Deploy on EVERY eligible market where expected_reward > expected_fill_cost
#   - Per-market notional = cost-to-score (min_size × midpoint × 2 + buffer)
#   - Total notional NOT bounded by allocator — bounded by Polymarket's
#     collateral-rebalance auto-cancel mechanism (Rule 2 design point: 3-8×
#     wallet notional from many simultaneous orders, only one fills at a time)
#   - Target market count 50-200 in steady state; soft sanity cap at 500
#     prevents runaway from API anomalies (Polymarket lists ~5k markets)
#
# Class name "SimpleAllocator" retained for import-site compatibility; the
# semantics are OverCommitAllocator per FX-052/053. See the OverCommitAllocator
# docstring on `compute()` for the design.
#
# All thresholds cfg-driven for runtime tuning per config_overrides.json.
def MIN_DAILY_RATE_USD(): return cfg("RF_OVERCOMMIT_MIN_DAILY_RATE_USD")
def MIN_EXPECTED_PER_MARKET(): return cfg("RF_OVERCOMMIT_MIN_EXPECTED_PER_MARKET")
def MAX_DEPLOYED_MARKETS(): return cfg("RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS")
def PER_MARKET_BUFFER_FRAC(): return cfg("RF_OVERCOMMIT_PER_MARKET_BUFFER_FRAC")
def EXPECTED_FILL_COST_FRAC(): return cfg("RF_OVERCOMMIT_EXPECTED_FILL_COST_FRAC")
def Q_SHARE_CONSERVATIVE_FACTOR(): return cfg("RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR")
# FX-090: allocator-side adverse-selection / time-to-event filter knobs.
def ALLOC_MIN_HOURS_TO_RESOLUTION(): return cfg("RF_ALLOC_MIN_HOURS_TO_RESOLUTION")
def ALLOC_MIN_HOURS_TO_GAME_START(): return cfg("RF_ALLOC_MIN_HOURS_TO_GAME_START")
def ALLOC_MAX_TIMING_FETCHES(): return cfg("RF_ALLOC_MAX_TIMING_FETCHES")
def ALLOC_TIMING_CACHE_TTL_SEC(): return cfg("RF_ALLOC_TIMING_CACHE_TTL_SEC")
# FX-093: allocator-side recent-volatility exclusion knobs.
def ALLOC_MAX_RECENT_VOLATILITY(): return cfg("RF_ALLOC_MAX_RECENT_VOLATILITY")
def ALLOC_VOLATILITY_WINDOW_HOURS(): return cfg("RF_ALLOC_VOLATILITY_WINDOW_HOURS")
def ALLOC_VOLATILITY_MIN_SAMPLES(): return cfg("RF_ALLOC_VOLATILITY_MIN_SAMPLES")
COLD_START_Q_SHARE = 0.005         # FX-086 (closes FX-064): DEFAULT mirror of the cfg knob RF_COLD_START_Q_SHARE. The live path (estimate_q_share) reads cfg("RF_COLD_START_Q_SHARE") so the prior is runtime-tunable via config_overrides.json (FX-046 says it is 24-94x off and it binds the EV gate). This module constant is retained as the compile-time default + for import-site arithmetic in tests; keep the two values in sync.

# FX-056: Extreme-price filter. Markets with midpoint < 0.10 or > 0.90
# produce wide effective spreads on dump. The 2026-05-25 fill on
# 0x46c09232 (NO at $0.08, dumped at $0.07) took 13.3% slippage vs
# 1-2% on mid-priced markets. Polymarket's reward formula doesn't
# discount these — they often have HIGH daily_rate that attracts the
# scorer — but the per-fill cost negates the per-cycle reward.
EXTREME_PRICE_LOW = 0.10
EXTREME_PRICE_HIGH = 0.90

# Kill switch thresholds — these replace SafetyController's 14 invariants
KILL_LOSS_FRAC = 0.10              # halt on 24h realized loss > 10% of wallet
KILL_DRAWDOWN_FRAC = 0.15          # halt on 15% drawdown from peak wallet

# Polymarket API
CLOB_HOST = "https://clob.polymarket.com"
USER_PCTS_PATH = "/rewards/user/percentages"
USER_TOTAL_PATH = "/rewards/user/total"
MARKETS_CURRENT_PATH = "/rewards/markets/current"
MARKET_DETAIL_PATH = "/markets/"        # FX-090: per-market timing (game_start_time, end_date_iso)


@dataclass
class CandidateMarket:
    """One reward-eligible market under consideration."""
    condition_id: str
    yes_tid: str
    no_tid: str
    daily_rate: float
    max_spread: float           # in basis points per Polymarket convention (e.g., 4.5 = 4.5¢)
    min_size: int
    midpoint_guess: float = 0.5  # used to estimate cost_per_share if no book fetched
    # FX-090: time-to-event fields (from CLOB /markets/{cid}; "" = unknown).
    # Pre-populatable by a test or a future bulk source to skip enrichment.
    game_start_time: str = ""
    end_date_iso: str = ""

    # Filled in by SimpleAllocator
    expected_q_share: float = 0.0
    expected_daily_reward: float = 0.0
    q_share_source: str = ""    # "api" | "cumulative" | "cold_start"
    target_shares: int = 0
    target_capital: float = 0.0
    timing_excluded_reason: str = ""  # FX-090: set when the time-to-event filter avoids this market


@dataclass
class AllocationResult:
    """Output of SimpleAllocator.compute()."""
    deploys: list[CandidateMarket]
    avoids: list[CandidateMarket]
    total_capital: float
    capital_deployed: float
    expected_total_reward: float
    kill_switch: bool = False
    kill_reason: str = ""
    sources_used: dict = field(default_factory=dict)  # {api: N, cumulative: N, cold_start: N}


class SimpleAllocator:
    """Discover → estimate q_share → rank → allocate, with minimal kill switch.

    Designed to be the entire decision layer above the I/O layer
    (market_discovery, order_lifecycle placement, dump_manager, guardrails).
    No state machine, no calibration, no β/η, no bandit.
    """

    def __init__(
        self,
        db_path: str,
        wallet_address: str,
        funder: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        *,
        # Override hooks for testability
        _now: Optional[callable] = None,
        _http: Optional[callable] = None,
        _timing_provider: Optional[callable] = None,
    ):
        self.db_path = db_path
        self.wallet_address = wallet_address
        self.funder = funder
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self._now = _now or time.time
        self._http = _http or requests.get
        # FX-090: optional (cid) -> (game_start_time, end_date_iso) provider for
        # tests/bulk sources; defaults to a CLOB /markets/{cid} fetch via _http.
        self._timing_provider = _timing_provider
        # FX-090: per-cid timing cache {cid: (fetched_at, game_start, end_date)}.
        self._timing_cache: dict[str, tuple] = {}

    # ── Polymarket API integration ──

    def _auth_headers(self, method: str, path: str) -> dict:
        """Build CLOB L2 auth headers for an authenticated endpoint."""
        ts = str(int(self._now()))
        msg = ts + method + path
        sig = base64.urlsafe_b64encode(
            hmac.new(
                base64.urlsafe_b64decode(self.api_secret),
                msg.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return {
            "POLY_API_KEY": self.api_key,
            "POLY_ADDRESS": self.wallet_address,
            "POLY_SIGNATURE": sig,
            "POLY_PASSPHRASE": self.api_passphrase,
            "POLY_TIMESTAMP": ts,
        }

    def fetch_current_q_shares(self) -> dict[str, float]:
        """Get real q_share (in PERCENT units) for currently-positioned markets.

        Verified empirically: API value is in percent (value/100 = fraction).
        Cross-checked against /rewards/user/markets earning data (Phase 0).

        Returns dict {condition_id: q_share_fraction}.
        Returns empty dict on API failure (caller falls through to Priority 1).
        """
        try:
            headers = self._auth_headers("GET", USER_PCTS_PATH)
            r = self._http(
                f"{CLOB_HOST}{USER_PCTS_PATH}",
                params={"signature_type": 2},
                headers=headers,
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"q_share API status={r.status_code}")
                return {}
            raw = r.json()
            # Values are in percent units — convert to fraction
            return {cid: float(v) / 100.0 for cid, v in raw.items()}
        except Exception as e:
            log.warning(f"q_share API error: {e}")
            return {}

    def fetch_reward_markets(self) -> list[CandidateMarket]:
        """Fetch all reward-eligible markets from Polymarket.

        Returns list of CandidateMarket (unfiltered).
        """
        markets: list[CandidateMarket] = []
        cursor = None
        for _ in range(20):  # safety bound on pagination
            params = {"limit": 500}
            if cursor:
                params["next_cursor"] = cursor
            try:
                r = self._http(
                    f"{CLOB_HOST}{MARKETS_CURRENT_PATH}",
                    params=params,
                    timeout=15,
                )
                if r.status_code != 200:
                    log.warning(f"markets/current status={r.status_code}")
                    break
                data = r.json()
            except Exception as e:
                log.warning(f"markets/current error: {e}")
                break
            page = data.get("data", []) if isinstance(data, dict) else data
            for m in page:
                try:
                    # FX-056: try to extract a midpoint hint from the API
                    # response so the downstream extreme-price filter has
                    # real data. The /rewards/markets/current schema doesn't
                    # guarantee a `tokens` field — when absent we fall back
                    # to the dataclass default 0.5 (no filter applied for
                    # that market). Same field shape used by market_discovery
                    # and reward_farmer when consuming /markets/{cid}.
                    midpoint_hint = 0.5
                    tokens = m.get("tokens", []) or []
                    if isinstance(tokens, list) and len(tokens) >= 1:
                        try:
                            p = float(tokens[0].get("price", 0.5))
                            if 0 < p < 1:
                                midpoint_hint = p
                        except (TypeError, ValueError):
                            pass

                    markets.append(
                        CandidateMarket(
                            condition_id=m["condition_id"],
                            yes_tid=m.get("yes_token_id", ""),
                            no_tid=m.get("no_token_id", ""),
                            daily_rate=float(m.get("native_daily_rate", m.get("total_daily_rate", 0)) or 0),
                            max_spread=float(m.get("rewards_max_spread", 4.5) or 4.5),
                            min_size=int(m.get("rewards_min_size", 20) or 20),
                            midpoint_guess=midpoint_hint,
                        )
                    )
                except (KeyError, ValueError, TypeError) as e:
                    log.debug(f"skip malformed market: {e}")
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor or cursor == "LTE=":
                break
        return markets

    # ── Q-share estimation (3-tier priority) ──

    def load_cumulative_ratios(self) -> dict[str, float]:
        """Read reward_market_stats for cumulative q_share ratio per cid.

        Returns {condition_id: ratio} where ratio = total_q_score / total_market_q.
        Verified Phase 0: this is 1.7× under the API truth (close, not catastrophic).
        """
        ratios: dict[str, float] = {}
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.execute(
                "SELECT condition_id, "
                "  json_extract(data, '$.total_q_score'), "
                "  json_extract(data, '$.total_market_q'), "
                "  json_extract(data, '$.q_score_samples') "
                "FROM reward_market_stats"
            )
            for row in cur:
                cid, q, mq, n = row
                if q is None or mq is None or not mq or float(mq) <= 0:
                    continue
                if n is None or int(n) < 10:
                    continue  # not enough samples to trust
                ratio = float(q) / float(mq)
                if 0 < ratio < 0.5:  # poisoned-row guard (FX-005 era)
                    ratios[cid] = ratio
            conn.close()
        except Exception as e:
            log.warning(f"cumulative ratios load failed: {e}")
        return ratios

    def estimate_q_share(
        self,
        cid: str,
        api_shares: dict[str, float],
        cumulative: dict[str, float],
    ) -> tuple[float, str]:
        """Pick q_share + tag the source. Returns (fraction, source_label)."""
        if cid in api_shares:
            return api_shares[cid], "api"
        if cid in cumulative:
            return cumulative[cid], "cumulative"
        # FX-086 (closes FX-064): cfg-driven, runtime-tunable. Defaults to
        # COLD_START_Q_SHARE (0.005) when no override is set.
        return cfg("RF_COLD_START_Q_SHARE"), "cold_start"

    # ── Allocation logic ──

    def _est_cost_per_market(self, m: CandidateMarket) -> float:
        """Estimate USD cost for full deploy (both sides) at min_size.

        FX-052: this is the COST-TO-SCORE under overcommit operation.
        cost ≈ min_size × cost_per_share × 2_sides × (1 + buffer)
        Per ground rules Rule 1, each market is sized to the MINIMUM that
        earns rewards (typically min_size shares), not the maximum that fits
        the budget. Buffer covers tick rounding + price drift.
        """
        # Both sides each cost ~midpoint per share; total ≈ 2 × midpoint × shares.
        # Use min(midpoint, 1-midpoint) for the per-side BUY cost.
        cost_per_share = max(0.10, min(m.midpoint_guess, 1.0 - m.midpoint_guess) * 2.0)
        base_cost = m.min_size * cost_per_share
        return base_cost * (1.0 + PER_MARKET_BUFFER_FRAC())

    def _estimate_fill_cost(self, m: CandidateMarket, position_notional: float) -> float:
        """FX-052/053: expected USD cost incurred IF the market fills today.

        Cost = position_notional × slippage_assumption. The slippage assumption
        is cfg-driven (default 2% per FX-056 extreme-price filter narrowing
        the population to mid-priced markets where slippage is ~1-3%). This
        is used to filter markets where (expected_reward_per_day × q_share)
        does not clear the per-fill cost — i.e., negative-EV deploys per
        ground rules Rule 1.

        Conservative: assumes 1 fill per day (the rate-limit target). At
        higher actual fill rates the per-market ROI drops; FX-051's
        per-market cooldown catches that.
        """
        return position_notional * EXPECTED_FILL_COST_FRAC()

    # ── FX-090: time-to-event (adverse-selection) enrichment + filter ──
    #
    # The candidate source (/rewards/markets/current) carries neither the
    # market resolution date (its end_date is a far-future reward-program
    # sentinel, e.g. 2500-12-31) nor game_start_time. CLOB /markets/{cid}
    # carries both. We enrich only the ranked candidates we'd actually deploy
    # (until the deploy cap fills), cache the ~immutable result, and exclude
    # markets too close to a decisive event. Fail-open everywhere: a market
    # whose timing can't be fetched/parsed is NOT excluded — the farmer's live
    # EXPIRY SWEEP + RF_SPORTS/GAME_BLOCK remain the real-time backstop.

    def _fetch_timing(self, cid: str) -> tuple[str, str]:
        """Return (game_start_time, end_date_iso) for one market; ("","") on failure."""
        if self._timing_provider is not None:
            try:
                gs, ed = self._timing_provider(cid)
                return (gs or "", ed or "")
            except Exception as e:
                log.debug(f"timing provider error {cid[:10]}: {e}")
                return ("", "")
        try:
            r = self._http(f"{CLOB_HOST}{MARKET_DETAIL_PATH}{cid}", timeout=10)
            if getattr(r, "status_code", 0) != 200:
                return ("", "")
            m = r.json()
            return (
                str(m.get("game_start_time", "") or ""),
                str(m.get("end_date_iso", "") or ""),
            )
        except Exception as e:
            log.debug(f"timing fetch error {cid[:10]}: {e}")
            return ("", "")

    def _get_timing(self, m: "CandidateMarket", budget: dict) -> None:
        """Populate m.game_start_time / m.end_date_iso — cached, budget-bounded.

        No-op if timing is already present (pre-populated), if enrichment is
        disabled (RF_ALLOC_MAX_TIMING_FETCHES <= 0), if BOTH hour-floors are
        disabled (nothing to filter on), or if the per-cycle fetch budget is
        spent (fail-open).
        """
        if m.end_date_iso or m.game_start_time:
            return
        if ALLOC_MAX_TIMING_FETCHES() <= 0:
            return
        if ALLOC_MIN_HOURS_TO_RESOLUTION() <= 0 and ALLOC_MIN_HOURS_TO_GAME_START() <= 0:
            return
        cid = m.condition_id
        now = self._now()
        ttl = ALLOC_TIMING_CACHE_TTL_SEC()
        cached = self._timing_cache.get(cid)
        if cached and (ttl <= 0 or (now - cached[0]) < ttl):
            m.game_start_time, m.end_date_iso = cached[1], cached[2]
            return
        if budget["fetches"] >= ALLOC_MAX_TIMING_FETCHES():
            budget["exhausted"] = True
            return
        gs, ed = self._fetch_timing(cid)
        budget["fetches"] += 1
        if gs or ed:  # cache only successful lookups so transient failures retry
            self._timing_cache[cid] = (now, gs, ed)
        m.game_start_time, m.end_date_iso = gs, ed

    def _timing_excluded(self, m: "CandidateMarket") -> tuple[bool, str]:
        """True if too close to a decisive event (resolution or game start).

        Pure function of m's timing fields + cfg + now. Fail-open: unparseable
        or absent timing is never excluded.
        """
        from datetime import datetime, timezone
        now_dt = datetime.fromtimestamp(self._now(), tz=timezone.utc)

        res_floor = ALLOC_MIN_HOURS_TO_RESOLUTION()
        if m.end_date_iso and res_floor and res_floor > 0:
            try:
                dt = datetime.fromisoformat(m.end_date_iso.replace("Z", "+00:00"))
                hrs = (dt - now_dt).total_seconds() / 3600.0
                if hrs < res_floor:
                    return True, f"resolves_in_{hrs:.1f}h<{res_floor:.0f}h"
            except (ValueError, TypeError):
                pass

        game_floor = ALLOC_MIN_HOURS_TO_GAME_START()
        if m.game_start_time and game_floor and game_floor > 0:
            try:
                dt = datetime.fromisoformat(m.game_start_time.replace("Z", "+00:00"))
                hrs = (dt - now_dt).total_seconds() / 3600.0
                if hrs < game_floor:
                    return True, f"game_in_{hrs:.1f}h<{game_floor:.0f}h"
            except (ValueError, TypeError):
                pass

        return False, ""

    def _recent_volatility(self, cid: str) -> Optional[float]:
        """FX-093: recent midpoint RANGE (max-min) for `cid` from book_snapshots
        over the volatility window, or None when there's not enough signal.

        Reads the per-market midpoint series the farmer logs to book_snapshots
        (shared DB). Returns None (fail-open — caller does NOT exclude) when the
        filter is disabled, there are fewer than ALLOC_VOLATILITY_MIN_SAMPLES
        snapshots in the window, or any DB error (e.g. the table is absent in a
        :memory: test DB). Range, not stdev — a single large news jump IS the
        signal we want to catch.
        """
        if ALLOC_MAX_RECENT_VOLATILITY() <= 0:
            return None
        since = self._now() - ALLOC_VOLATILITY_WINDOW_HOURS() * 3600.0
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT MAX(midpoint), MIN(midpoint), COUNT(*) "
                    "FROM book_snapshots WHERE condition_id = ? AND ts >= ?",
                    (cid, since),
                ).fetchone()
            finally:
                conn.close()
        except Exception as e:
            log.debug(f"volatility query failed {cid[:10]}: {e}")
            return None
        if not row or row[0] is None or row[2] is None:
            return None
        if int(row[2]) < ALLOC_VOLATILITY_MIN_SAMPLES():
            return None  # too few samples — insufficient signal, fail-open
        return float(row[0]) - float(row[1])

    def compute(
        self,
        wallet_usd: float,
        wallet_peak_usd: float,
        wallet_24h_ago_usd: Optional[float],
        realized_loss_24h: float,
        markets: Optional[list[CandidateMarket]] = None,
        excluded_cids: Optional[set[str]] = None,
        size_reduction_cids: Optional[set[str]] = None,
        global_tighten: bool = False,
        global_reward_low: bool = False,
        q_share_distrust_cids: Optional[set[str]] = None,
    ) -> AllocationResult:
        """Main allocation entry point.

        Args:
            wallet_usd: current wallet balance
            wallet_peak_usd: highest wallet observed (for drawdown calc)
            wallet_24h_ago_usd: wallet 24h ago (or None if unknown)
            realized_loss_24h: sum of negative pnl from unwinds in last 24h (positive USD)
            markets: optional override (for testing); if None, fetches from Polymarket
            excluded_cids: FX-051 cooldown filter — set of condition_ids the
              DecisionPolicy has put on cooldown after recent losses. Pass
              `None` (or empty set) when the ROI tracker isn't running.
            size_reduction_cids: P4 of 9/10 plan — Ground Rule 3 trigger #3.
              Set of cids whose fill_rate > target (>1/hr default). For these
              markets, target_shares is halved this cycle to stay on the
              market with smaller exposure rather than cooling entirely.
              Pass `None` (or empty set) when the trigger isn't running.
            global_tighten: P4 of 9/10 plan — Ground Rule 3 trigger #5.
              When True (global loss > rewards detected), allocator raises
              the MIN_DAILY_RATE_USD floor 2× AND applies a global 0.5×
              size multiplier — biases toward fewer, safer deploys until
              the rolling metric recovers. Default False (no tightening).

        Returns AllocationResult with deploys, avoids, and kill signal.
        """
        sources_used = {"api": 0, "cumulative": 0, "cold_start": 0}
        excluded = excluded_cids or set()
        size_reduction = size_reduction_cids or set()
        q_distrust = q_share_distrust_cids or set()

        # ── Kill switch check FIRST ──
        kill, reason = self.check_kill_switch(
            wallet_usd, wallet_peak_usd, realized_loss_24h
        )
        if kill:
            return AllocationResult(
                deploys=[], avoids=[],
                total_capital=wallet_usd, capital_deployed=0,
                expected_total_reward=0,
                kill_switch=True, kill_reason=reason,
                sources_used=sources_used,
            )

        # ── Discover + estimate ──
        candidates = markets if markets is not None else self.fetch_reward_markets()
        api_shares = self.fetch_current_q_shares()
        cumulative = self.load_cumulative_ratios()

        # FX-046 conservative margin (P3): API q_share is Polymarket's own
        # measurement (ground truth, no margin). Cumulative + cold-start are
        # heuristics that the FX-046 investigation showed under-predict by
        # 24-94×. Default conservative factor is 1.0 (no-op — accept the
        # uncertainty and let FX-051 cooldowns catch losers). Operators
        # concerned about over-deployment can set RF_OVERCOMMIT_Q_SHARE_CONSERVATIVE_FACTOR
        # below 1.0 to bias non-API expected_reward down → EV gate tightens.
        # FX-061 (P11): for cids in `q_distrust` (recent API-vs-cumulative
        # divergence > 2×), apply an ADDITIONAL 0.5× factor to non-API
        # q_share. Compounds multiplicatively with the global conservative
        # factor if both are < 1.0. Implements ground_rules.md §3 trigger
        # #6 action "recalibrate scoring": when bot's heuristic disagrees
        # with Polymarket's truth, distrust the heuristic for that cid.
        conservative_factor = Q_SHARE_CONSERVATIVE_FACTOR()
        distrust_factor = 0.5
        for m in candidates:
            q, src = self.estimate_q_share(m.condition_id, api_shares, cumulative)
            # Apply conservative factor ONLY to non-API sources
            if src != "api":
                q = q * conservative_factor
                # P11: per-cid distrust factor (only meaningful when allocator
                # falls back to non-API source for a cid we KNOW has API/cumul
                # disagreement). If API IS available for this cid right now,
                # this branch never executes (src=="api" above).
                if m.condition_id in q_distrust:
                    q = q * distrust_factor
            m.expected_q_share = q
            m.expected_daily_reward = m.daily_rate * q
            m.q_share_source = src
            sources_used[src] += 1

        # ── Filter (FX-052/053 OverCommitAllocator) ──
        # NOTE: /rewards/markets/current does NOT include yes_token_id /
        # no_token_id fields. They live in /markets/{cid}. The farmer's
        # reward_farmer.py:970-987 has a CLOB fallback that fetches token_ids
        # if missing from the alloc — so we do NOT filter on yes_tid/no_tid
        # here. Doing so would zero out every market.
        # P4 Trigger #5: under global_tighten, raise MIN_DAILY_RATE_USD floor
        # 2× to skip the long tail of low-reward markets. Symmetric with the
        # global_size_factor below — both effects accumulate to bias toward
        # fewer, safer deploys until the global loss/reward ratio recovers.
        # P10 Trigger #4 (FX-060): under global_reward_low (rewards under
        # target, NOT losing), HALVE both floors to widen the candidate set
        # per ground_rules.md "expand market count, lower per-market
        # expected-reward floor". Mutually exclusive with global_tighten
        # (decision_policy ensures only one fires). Applies only when
        # actually under target — default no-op.
        rate_multiplier = 1.0
        expected_multiplier = 1.0
        if global_tighten:
            rate_multiplier = 2.0
        elif global_reward_low:
            rate_multiplier = 0.5
            expected_multiplier = 0.5
        min_rate = MIN_DAILY_RATE_USD() * rate_multiplier
        min_expected = MIN_EXPECTED_PER_MARKET() * expected_multiplier
        eligible = [
            m for m in candidates
            if m.daily_rate >= min_rate
            and m.expected_daily_reward >= min_expected
            # FX-056: skip extreme-price markets (midpoint < 0.10 or > 0.90)
            # where dump slippage routinely exceeds per-cycle reward. Markets
            # without a price hint pass through (midpoint_guess default 0.5)
            # so this is fail-open — a follow-up filter at farmer-side book
            # fetch time will catch any that slip through.
            and EXTREME_PRICE_LOW <= m.midpoint_guess <= EXTREME_PRICE_HIGH
            # FX-051: skip markets in cooldown after recent losses (set passed
            # in from DecisionPolicy.get_excluded_cids()). Empty set passes
            # everything through, so the allocator stays usable when the ROI
            # tracker isn't wired up.
            and m.condition_id not in excluded
        ]

        # ── Rank by expected reward ──
        eligible.sort(key=lambda x: -x.expected_daily_reward)

        # ── OverCommit allocation (FX-052/053) ──
        # No total-notional budget — Polymarket's collateral-rebalance auto-
        # cancel mechanism handles overcommit per Ground Rule 2 (3-8× wallet
        # notional is the design point). Per-market cost is cost-to-score
        # (min_size × midpoint × 2 + buffer), NOT capped by a wallet
        # fraction. Each market deploys iff its expected_reward clears
        # expected_fill_cost (positive-EV gate per Ground Rule 1 + 3).
        max_deploys = MAX_DEPLOYED_MARKETS()  # SOFT sanity cap (default 500)
        deploys: list[CandidateMarket] = []
        avoids: list[CandidateMarket] = []
        used = 0.0
        positive_ev_count = 0
        # FX-090: time-to-event enrichment budget + counter for this cycle.
        timing_budget = {"fetches": 0, "exhausted": False}
        timing_excluded_count = 0
        vol_excluded_count = 0  # FX-093
        for m in eligible:
            if len(deploys) >= max_deploys:
                # Soft cap hit — extremely rare in practice (5k market pool
                # × eligibility filters typically yields 100-500 markets).
                # Logged via the SIMPLE_ALLOC telemetry line; operator can
                # raise RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS if needed.
                avoids.append(m)
                continue

            # FX-090: adverse-selection / time-to-event filter. Enrich this
            # ranked candidate with game_start_time + end_date_iso (cached,
            # budget-bounded) and skip markets too close to a decisive event —
            # the high-rate short-dated daily/news/sports markets the farmer's
            # own guards refuse to place (wasting deploy slots) and that court
            # adverse fills. Fail-open: unknown timing → not skipped.
            self._get_timing(m, timing_budget)
            t_excluded, t_reason = self._timing_excluded(m)
            if t_excluded:
                m.timing_excluded_reason = t_reason
                timing_excluded_count += 1
                avoids.append(m)
                continue

            # FX-093: recent-volatility filter. Exclude candidates whose
            # book_snapshots midpoint has swung more than the cap over the
            # window — news-active markets that adversely fill our resting
            # quotes. Triggered by book movement (no fill needed); fail-open
            # when there's insufficient history (FX-051 cooldown backstops).
            vol_cap = ALLOC_MAX_RECENT_VOLATILITY()
            if vol_cap and vol_cap > 0:
                recent_vol = self._recent_volatility(m.condition_id)
                if recent_vol is not None and recent_vol > vol_cap:
                    m.timing_excluded_reason = f"volatility_{recent_vol:.3f}>{vol_cap:.2f}"
                    vol_excluded_count += 1
                    avoids.append(m)
                    continue

            cost_per_market = self._est_cost_per_market(m)
            expected_fill_cost = self._estimate_fill_cost(m, cost_per_market)
            # Positive-EV gate: only deploy if expected reward clears fill cost.
            # m.expected_daily_reward is already in $/day; comparing against
            # per-fill cost assumes ≤1 fill per day per market (the operating
            # target per ground_rules.md; FX-051's fill-rate trigger catches
            # markets that exceed it).
            if m.expected_daily_reward < expected_fill_cost:
                avoids.append(m)
                continue
            positive_ev_count += 1

            # Compute shares from target capital
            cost_per_share = max(0.10, min(m.midpoint_guess, 1.0 - m.midpoint_guess) * 2.0)
            target_shares = max(m.min_size, int(cost_per_market / cost_per_share))

            # P4 Trigger #3: per-market size reduction for high fill_rate markets.
            # Halve target_shares (clamped at min_size for venue eligibility).
            # AND global_tighten (Trigger #5): apply 0.5× to ALL deploys this
            # cycle. Both effects compose multiplicatively when both fire.
            size_multiplier = 1.0
            if m.condition_id in size_reduction:
                size_multiplier *= 0.5
            if global_tighten:
                size_multiplier *= 0.5
            if size_multiplier < 1.0:
                target_shares = max(m.min_size, int(target_shares * size_multiplier))
                cost_per_market = target_shares * cost_per_share

            m.target_shares = target_shares
            m.target_capital = round(cost_per_market, 2)

            deploys.append(m)
            used += cost_per_market

        # Include non-eligible candidates as avoids (for telemetry / farmer visibility)
        non_eligible = [m for m in candidates if m not in eligible]
        avoids.extend(non_eligible)

        expected_total = sum(m.expected_daily_reward for m in deploys)
        notional_overcommit_ratio = (used / wallet_usd) if wallet_usd > 0 else 0.0

        if timing_budget.get("exhausted"):
            log.warning(
                f"[OVERCOMMIT_ALLOC] FX-090 timing-fetch budget "
                f"({ALLOC_MAX_TIMING_FETCHES()}) exhausted this cycle — remaining "
                f"candidates deployed WITHOUT the adverse-selection check (farmer "
                f"expiry sweep remains the backstop). Raise RF_ALLOC_MAX_TIMING_FETCHES "
                f"if this persists."
            )
        log.info(
            f"[OVERCOMMIT_ALLOC] eligible={len(eligible)} positive_ev={positive_ev_count} "
            f"deploys={len(deploys)} avoids={len(avoids)} "
            f"timing_excluded={timing_excluded_count} vol_excluded={vol_excluded_count} timing_fetches={timing_budget['fetches']} "
            f"notional_total=${used:.2f} wallet=${wallet_usd:.2f} "
            f"overcommit_ratio={notional_overcommit_ratio:.2f}× "
            f"(target band 3-8× per Ground Rule 2) "
            f"p4_size_reduction_cids={len(size_reduction)} "
            f"p4_global_tighten={global_tighten} "
            f"p10_global_reward_low={global_reward_low} "
            f"p11_q_distrust_cids={len(q_distrust)}"
        )

        return AllocationResult(
            deploys=deploys, avoids=avoids,
            total_capital=wallet_usd, capital_deployed=round(used, 2),
            expected_total_reward=round(expected_total, 4),
            kill_switch=False, kill_reason="",
            sources_used=sources_used,
        )

    # ── Minimal safety (replaces 14-invariant SafetyController) ──

    def check_kill_switch(
        self,
        wallet_usd: float,
        wallet_peak_usd: float,
        realized_loss_24h: float,
    ) -> tuple[bool, str]:
        """Two kill triggers: 24h realized loss > 10% wallet, or 15% drawdown.

        Returns (should_kill, reason).
        """
        if wallet_usd <= 0:
            return True, f"wallet collapsed to ${wallet_usd:.2f}"

        if realized_loss_24h > wallet_usd * KILL_LOSS_FRAC:
            return True, (
                f"24h realized loss ${realized_loss_24h:.2f} > "
                f"{KILL_LOSS_FRAC * 100:.0f}% of wallet ${wallet_usd:.2f}"
            )

        if wallet_peak_usd > 0:
            drawdown = 1.0 - (wallet_usd / wallet_peak_usd)
            if drawdown > KILL_DRAWDOWN_FRAC:
                return True, (
                    f"drawdown {drawdown * 100:.1f}% > "
                    f"{KILL_DRAWDOWN_FRAC * 100:.0f}% from peak ${wallet_peak_usd:.2f}"
                )

        return False, ""

    # ── Output serialization (matches existing schema for farmer compat) ──

    def write_allocation_json(
        self,
        result: AllocationResult,
        output_path: str = "market_allocations.json",
    ) -> None:
        """Write market_allocations.json in the format the farmer reads.

        Schema matches `oversight/allocation_writer.compute_allocations` output
        so the farmer requires zero changes when this module replaces the agent.
        """
        markets_json = []

        # Schema must match `oversight/allocation_writer._to_dict` output
        # so the farmer (reward_farmer.py:950-1000) reads the same fields.
        # Critical fields per the farmer audit:
        #   condition_id (mandatory; no fallback)
        #   action (filter "deploy" vs "avoid")
        #   shares_per_side (NOT "shares" — farmer reads this exact name)
        #   _total_capital (stamped per row; runtime guardrails depend on it)
        # Optional with fallbacks: daily_rate, min_size, max_spread, end_date_iso
        for m in result.deploys:
            markets_json.append({
                "condition_id": m.condition_id,
                "yes_tid": m.yes_tid,         # extra; farmer fetches from CLOB if missing
                "no_tid": m.no_tid,           # extra; farmer fetches from CLOB if missing
                "action": "deploy",
                "shares_per_side": m.target_shares,
                "daily_rate": m.daily_rate,
                "max_spread": m.max_spread / 100.0 if m.max_spread > 1 else m.max_spread,
                "min_size": m.min_size,
                "end_date_iso": m.end_date_iso or "",        # FX-090: populated when known (farmer no longer re-fetches)
                "game_start_time": m.game_start_time or "",  # FX-090: populated when known
                "score": round(m.expected_daily_reward, 6),
                "q_share_pct": round(m.expected_q_share, 6),
                "q_share_source": m.q_share_source,
                "est_capital_cost": m.target_capital,
                "expected_daily_reward": round(m.expected_daily_reward, 4),
                "_total_capital": round(result.total_capital, 2),  # guardrail stamp
            })

        for m in result.avoids:
            markets_json.append({
                "condition_id": m.condition_id,
                "action": "avoid",
                "shares_per_side": 0,
                "daily_rate": m.daily_rate,
                "max_spread": m.max_spread / 100.0 if m.max_spread > 1 else m.max_spread,
                "min_size": m.min_size,
                "q_share_pct": round(m.expected_q_share, 6),
                "q_share_source": m.q_share_source,
                "_total_capital": round(result.total_capital, 2),
            })

        # FX-052/053: overcommit ratio telemetry for monitoring. Ground
        # Rule 2 target band is 3-8× wallet notional. Persistently outside
        # the band indicates either (low end) the eligibility / EV filters
        # are too aggressive, or (high end) the soft sanity cap on market
        # count is binding (RF_OVERCOMMIT_MAX_DEPLOYED_MARKETS).
        overcommit_ratio = (
            result.capital_deployed / result.total_capital
            if result.total_capital > 0 else 0.0
        )

        payload = {
            "version": "simple-1.2",  # FX-052/053: OverCommitAllocator metadata
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "num_deploy": len(result.deploys),
            "num_avoid": len(result.avoids),
            "total_capital_deployed": result.capital_deployed,
            "total_capital": round(result.total_capital, 2),
            # FX-043: top-level metadata stamp so the farmer's
            # `_guardrail_total_capital_from_alloc` reader has a value
            # even on 0-deploy cycles (when no deploy row exists to
            # carry the per-row _total_capital stamp). Pre-FX-043 the
            # reader returned None on 0-deploy cycles → notional/cluster/
            # 24h-loss/CF guardrails ALL silently failed-open for the
            # duration. Observed 2026-05-21 19:50-19:54 UTC. The
            # underscore prefix matches the per-row field name so the
            # reader can use either source.
            "_total_capital": round(result.total_capital, 2),
            # FX-052/053: monitoring fields for the overcommit operating point.
            "_notional_overcommit_ratio": round(overcommit_ratio, 3),
            "_target_market_count_band": [50, 200],
            "expected_total_reward": result.expected_total_reward,
            "kill_switch": result.kill_switch,
            "kill_reason": result.kill_reason,
            "sources_used": result.sources_used,
            "markets": markets_json,
        }

        # Atomic write via temp file (matches existing pattern)
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, output_path)
