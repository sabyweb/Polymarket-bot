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
from volume_cache import lookup as volume_cache_lookup

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

# FIX-1 (RC-4): conservative same-day / event-resolution patterns (lowercase
# substring match on the market question). Tight by design — must catch the
# IPO-day / first-day / intraday markets that carry a wrong far-future or null
# end_date_iso (e.g. "SpaceX IPO closing market cap above $2T?",
# "...Closing Share Price Up/Down on First Day?"), WITHOUT catching legitimate
# far-dated markets ("...by December 31, 2026"). Deliberately omits bare "ipo"
# (a "Will X IPO by 2027?" market is genuinely far-dated). Only consulted when
# RF_ALLOC_EVENT_DATE_GUARD is on AND timing enrichment was attempted.
_EVENT_SAME_DAY_PATTERNS = (
    "on ipo day", "first day", "closing market cap", "closing share price",
    "intraday", "at market close", "market close on", "by market close",
    "end of day", "opening day",
)

# Discrete unscheduled-political-event patterns. These resolve on a sudden news event
# (announcement, agreement, signing, closure, strike) that has NO scheduled game_start_time,
# so the game-start filter is blind to them — they are the verified held-to-resolution losers
# (Iran/Trump/Israel). Deliberately CONSERVATIVE: action verbs + specific geopolitical nouns,
# chosen NOT to match continuous-metric markets ("USD reaches X rials", "margin between X-Y"),
# which are safe even without a start time. Used by _no_gamestart_event_excluded.
# Patterns are matched as substrings on the lowercased question, so they are written with
# leading spaces / specific phrasing to avoid substring false positives (e.g. " agree" not
# "agree" so "disagree" doesn't match; "military coup"/"coup d" not "coup" so "couple" doesn't;
# "to sign"/"signs a" not "sign a" so "design a" doesn't).
_DISCRETE_EVENT_PATTERNS = (
    "announce", " agrees", " agree to", " agree on", "will sign", "to sign", "signs a", "signs the",
    "ceasefire", "airspace", "enrichment", "summit", "meet with", "meeting be", "diplomatic",
    "memorandum", "peace deal", "reach a deal", "reach an agreement", "reach agreement",
    "strike on", "invade", "military coup", "coup d", "resign", "step down",
    "sanction", "treaty", "end the war", "close the strait", "blockade",
)
# Continuous-metric / level markets: SAFE even with no game_start_time — never excluded by the
# no-gamestart guard. Checked BEFORE the discrete-event list so a level market is always allowed.
# Deliberately excludes bare "reach " (matches "reach a deal", a discrete event) and "closing
# price" (IPO-ish, FIX-1's domain) to avoid wrongly allowing discrete-event markets.
_CONTINUOUS_METRIC_PATTERNS = (
    " between ", "be between", " above ", " below ", "gross margin", "exchange rate",
    "rials by", "rial by", "index be", " cpi ", "gdp", "unemployment rate",
)

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
    volume_24h: float = 0.0     # USD CLOB 24h volume from Gamma API
    expected_q_share: float = 0.0
    expected_daily_reward: float = 0.0
    q_share_source: str = ""    # "api" | "cumulative" | "cold_start"
    target_shares: int = 0
    target_capital: float = 0.0
    timing_excluded_reason: str = ""  # FX-090: set when the time-to-event filter avoids this market
    # FIX-1 (RC-4): event/same-day guard inputs, populated during timing enrichment.
    timing_fetch_attempted: bool = False  # True once enrichment actually tried to fetch this market's timing/status
    closed: bool = False                  # CLOB `closed` flag (market resolved/closed)
    accepting_orders: bool = True         # CLOB `accepting_orders` flag (False = book closed)
    question: str = ""                    # market question text (for same-day/event pattern match)
    event_guard_reason: str = ""          # FIX-1: set when the event-date guard avoids this market


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
    candidate_features: list = field(default_factory=list)  # A3 survivorship log (off by default); not serialized to the alloc JSON


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

        # A/B C1 volume filter + feature log need 24h CLOB volume. Use the
        # slug-based cache; on a warm cache this is a local SQLite lookup.
        # Fail-open: any error leaves volume_24h at 0.0.
        if markets and (cfg("RF_AB_EXPERIMENT_ENABLED") or cfg("RF_CANDIDATE_FEATURE_LOG_ENABLED")):
            try:
                vol_map = volume_cache_lookup(
                    [m.condition_id for m in markets],
                    db_path=self.db_path,
                    ttl=cfg("RF_VOLUME_CACHE_TTL_SEC"),
                    max_workers=cfg("RF_VOLUME_CACHE_MAX_WORKERS"),
                )
                for m in markets:
                    m.volume_24h = vol_map.get(m.condition_id, 0.0)
            except Exception as e:
                log.warning(f"volume_cache lookup failed: {e}")

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

    def _fetch_timing(self, cid: str) -> dict:
        """Return a market timing+status dict for one cid:
        {game_start_time, end_date_iso, closed, accepting_orders, question}.
        Safe defaults (blank dates, closed=False, accepting_orders=True, "") on
        failure — so a failed lookup never *causes* an exclusion (fail-open). The
        test `_timing_provider` hook still returns just (game_start_time,
        end_date_iso); its status fields fall back to the safe defaults.
        """
        base = {"game_start_time": "", "end_date_iso": "",
                "closed": False, "accepting_orders": True, "question": ""}
        if self._timing_provider is not None:
            try:
                gs, ed = self._timing_provider(cid)
                base["game_start_time"] = gs or ""
                base["end_date_iso"] = ed or ""
                return base
            except Exception as e:
                log.debug(f"timing provider error {cid[:10]}: {e}")
                return base
        try:
            r = self._http(f"{CLOB_HOST}{MARKET_DETAIL_PATH}{cid}", timeout=10)
            if getattr(r, "status_code", 0) != 200:
                return base
            m = r.json()
            return {
                "game_start_time": str(m.get("game_start_time", "") or ""),
                "end_date_iso": str(m.get("end_date_iso", "") or ""),
                "closed": bool(m.get("closed", False)),
                # CLOB omits accepting_orders on some payloads — default True so a
                # missing flag never triggers the guard (fail-open).
                "accepting_orders": bool(m.get("accepting_orders", True)),
                "question": str(m.get("question", "") or ""),
            }
        except Exception as e:
            log.debug(f"timing fetch error {cid[:10]}: {e}")
            return base

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
        # Both date-floors disabled normally means "nothing to filter on" → skip.
        # But the FIX-1 event-date guard reuses this same enrichment fetch, so keep
        # enriching when it is enabled even if the hour-floors are off.
        if (ALLOC_MIN_HOURS_TO_RESOLUTION() <= 0 and ALLOC_MIN_HOURS_TO_GAME_START() <= 0
                and not bool(cfg("RF_ALLOC_EVENT_DATE_GUARD"))):
            return
        cid = m.condition_id
        now = self._now()
        ttl = ALLOC_TIMING_CACHE_TTL_SEC()
        cached = self._timing_cache.get(cid)
        if cached and (ttl <= 0 or (now - cached[0]) < ttl):
            self._apply_timing_meta(m, cached[1])
            return
        if budget["fetches"] >= ALLOC_MAX_TIMING_FETCHES():
            budget["exhausted"] = True
            return
        meta = self._fetch_timing(cid)
        budget["fetches"] += 1
        self._apply_timing_meta(m, meta)
        # cache only lookups that returned something so transient failures retry
        if meta.get("game_start_time") or meta.get("end_date_iso") or meta.get("question"):
            self._timing_cache[cid] = (now, meta)

    def _ab_cohort(self, cid: str) -> int:
        """A/B experiment cohort for a market: stable hash(condition_id) % count.

        Deterministic + pseudo-random so each cohort sees a representative market
        mix (assignment is fixed per market, never drifts). Returns 0 (baseline)
        when the experiment is off or the count is <= 1. Cohort is a pure function
        of condition_id, so the offline analyzer can recompute it without any stored
        column. See docs/AB_RESUME_DESIGN.md.
        """
        import hashlib
        from config import cfg
        try:
            n = int(cfg("RF_AB_COHORT_COUNT") or 1)
        except (TypeError, ValueError):
            n = 1
        if n <= 1:
            return 0
        h = int(hashlib.sha1(cid.encode("utf-8")).hexdigest(), 16)
        return h % n

    def _effective_target_queue_usd(self, cid: str) -> float:
        """Decision-time queue-ahead target used by order_lifecycle for this cid.

        Baseline (C0) uses RF_TARGET_QUEUE_AHEAD_USD. Trader cohorts (C1/C2)
        use RF_AB_C1_TARGET_QUEUE_AHEAD_USD when the experiment is enabled.
        """
        if cfg("RF_AB_EXPERIMENT_ENABLED") and self._ab_cohort(cid) != 0:
            try:
                trader_target = float(cfg("RF_AB_C1_TARGET_QUEUE_AHEAD_USD") or 0.0)
            except (TypeError, ValueError):
                trader_target = 0.0
            if trader_target > 0:
                return trader_target
        return cfg("RF_TARGET_QUEUE_AHEAD_USD")

    def _hours_to_resolution(self, end_date_iso: str) -> float | None:
        """Hours from now to end_date_iso, or None if unparseable/absent.

        Uses the allocator's injected clock so tests with a frozen _now get
        deterministic values.
        """
        if not end_date_iso:
            return None
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
            now_dt = datetime.fromtimestamp(self._now(), tz=timezone.utc)
            return (dt - now_dt).total_seconds() / 3600.0
        except (ValueError, TypeError):
            return None

    def _apply_timing_meta(self, m: "CandidateMarket", meta: dict) -> None:
        """Copy a _fetch_timing dict onto the candidate + mark enrichment attempted."""
        m.game_start_time = meta.get("game_start_time", "") or ""
        m.end_date_iso = meta.get("end_date_iso", "") or ""
        m.closed = bool(meta.get("closed", False))
        m.accepting_orders = bool(meta.get("accepting_orders", True))
        m.question = meta.get("question", "") or m.question
        m.timing_fetch_attempted = True

    def _timing_excluded(self, m: "CandidateMarket") -> tuple[bool, str]:
        """True if too close to a decisive event (resolution or game start).

        Pure function of m's timing fields + cfg + now. Fail-open: unparseable
        or absent timing is never excluded.
        """
        from datetime import datetime, timezone
        now_dt = datetime.fromtimestamp(self._now(), tz=timezone.utc)

        res_floor = ALLOC_MIN_HOURS_TO_RESOLUTION()
        # A/B trader cohorts (C1/C2): looser resolution guard.
        if cfg("RF_AB_EXPERIMENT_ENABLED") and self._ab_cohort(m.condition_id) != 0:
            try:
                trader_res_floor = float(cfg("RF_AB_C1_MIN_HOURS_TO_RESOLUTION") or 0.0)
            except (TypeError, ValueError):
                trader_res_floor = 0.0
            if trader_res_floor > 0:
                res_floor = trader_res_floor
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

    def _event_guard_excluded(self, m: "CandidateMarket") -> tuple[bool, str]:
        """FIX-1 (RC-4): exclude event/same-day markets whose end_date_iso is an
        unreliable sentinel/null, which `_timing_excluded` can't catch.

        Off by default (RF_ALLOC_EVENT_DATE_GUARD). Acts ONLY on candidates whose
        timing was actually enriched this cycle (`timing_fetch_attempted`), so it
        never fires for the un-enriched tail — un-enriched markets stay fail-open,
        preserving Ground Rule 1 coverage. Two signals:
          (a) the CLOB reports the market closed / not accepting orders — definitive;
          (b) the question matches a conservative same-day/event pattern (e.g. the
              SpaceX IPO-day suite, which carried end_date=2027 or None).
        Single-axis: timing exclusion only — no effect on sizing, ranking, or EV.
        """
        if not bool(cfg("RF_ALLOC_EVENT_DATE_GUARD")):
            return False, ""
        if not m.timing_fetch_attempted:
            return False, ""  # never enriched → unknown timing, stay fail-open
        if m.closed:
            return True, "event_guard:closed"
        if not m.accepting_orders:
            return True, "event_guard:not_accepting_orders"
        q = (m.question or "").lower()
        if q:
            for pat in _EVENT_SAME_DAY_PATTERNS:
                if pat in q:
                    return True, f"event_guard:same_day[{pat}]"
        return False, ""

    def _no_gamestart_event_excluded(self, m: "CandidateMarket") -> tuple[bool, str]:
        """Verified-2026-06-17 guard: exclude DISCRETE unscheduled-political-event markets that
        carry NO game_start_time. Such markets (Iran/Trump/Israel "will X happen by date") have an
        unknowable catalyst, so a resting quote gets run over when the news lands and we end up
        holding to resolution at a loss — they are the source of the −$99/14d held-to-resolution
        bucket, and the existing game-start filter is structurally blind to them (0% have a start).

        Off by default (RF_ALLOC_NO_GAMESTART_EVENT_GUARD). Like the other timing guards it acts ONLY
        on enriched candidates (`timing_fetch_attempted`) — un-enriched markets stay fail-open so
        Ground Rule 1 coverage is preserved. Decision order, all fail-SAFE (do NOT exclude) on miss:
          1) has a game_start_time            -> NOT excluded (the game-start filter handles it)
          2) question matches a CONTINUOUS-metric pattern -> NOT excluded (safe level market)
          3) question matches a DISCRETE-event pattern    -> EXCLUDE
        Pure timing/text function: no effect on sizing, ranking, or EV (single-axis).
        """
        if not bool(cfg("RF_ALLOC_NO_GAMESTART_EVENT_GUARD")):
            return False, ""
        if not m.timing_fetch_attempted:
            return False, ""                       # unknown timing -> fail-open (coverage)
        if (m.game_start_time or "").strip():
            return False, ""                       # scheduled -> game-start filter's job
        q = (m.question or "").lower()
        if not q:
            return False, ""
        if any(p in q for p in _CONTINUOUS_METRIC_PATTERNS):
            return False, ""                       # safe continuous/level market
        for pat in _DISCRETE_EVENT_PATTERNS:
            if pat in q:
                return True, f"no_gamestart_event[{pat}]"
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

    def _recent_sweep_rate(self, cid: str) -> Optional[float]:
        """Book SWEEP RATE for `cid`: fraction of consecutive book_snapshots whose midpoint moved
        more than RF_ALLOC_SWEEP_JUMP_USD over the volatility window. Verified 2026-06-17 to separate
        net-good (≈0 sweeps) from net-bad markets (J≈0.49) — the behavioural form of the "calm book"
        rule. Returns None (fail-open — caller does NOT exclude) when the filter is disabled, there
        are fewer than ALLOC_VOLATILITY_MIN_SAMPLES snapshots, or on any DB error. Reuses the
        volatility window/min-samples so it shares the existing book-coverage characteristics.
        """
        try:
            cap = float(cfg("RF_ALLOC_MAX_RECENT_SWEEP_RATE") or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap <= 0:
            return None
        try:
            jump = float(cfg("RF_ALLOC_SWEEP_JUMP_USD") or 0.02)
        except (TypeError, ValueError):
            jump = 0.02
        since = self._now() - ALLOC_VOLATILITY_WINDOW_HOURS() * 3600.0
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT midpoint FROM book_snapshots WHERE condition_id = ? AND ts >= ? ORDER BY ts",
                    (cid, since),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            log.debug(f"sweep query failed {cid[:10]}: {e}")
            return None
        mids = [float(r[0]) for r in rows if r and r[0] is not None]
        if len(mids) < ALLOC_VOLATILITY_MIN_SAMPLES():
            return None  # insufficient signal — fail-open
        jumps = sum(1 for i in range(1, len(mids)) if abs(mids[i] - mids[i - 1]) > jump)
        return jumps / max(1, len(mids) - 1)

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
        portfolio_value_usd: Optional[float] = None,
        portfolio_peak_usd: Optional[float] = None,
    ) -> AllocationResult:
        """Main allocation entry point.

        Args:
            wallet_usd: current wallet balance (cash)
            wallet_peak_usd: legacy peak alias; used when portfolio_peak omitted
            portfolio_value_usd: FX-095 cash+inventory mark (defaults to wallet_usd)
            portfolio_peak_usd: FX-095 peak total portfolio (defaults to wallet_peak_usd)
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
        pval = portfolio_value_usd if portfolio_value_usd is not None else wallet_usd
        ppeak = portfolio_peak_usd if portfolio_peak_usd is not None else wallet_peak_usd
        kill, reason = self.check_kill_switch(
            wallet_usd=wallet_usd,
            portfolio_value_usd=pval,
            portfolio_peak_usd=ppeak,
            realized_loss_24h=realized_loss_24h,
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

        # ── Rank by expected reward (FX-097 Phase 5c: stability-weighted) ──
        vol_penalty_k = 0.0
        try:
            from config import cfg
            vol_penalty_k = float(cfg("RF_RANK_VOL_PENALTY_K") or 0.0)
        except Exception:
            pass

        def _rank_key(m: CandidateMarket) -> float:
            score = m.expected_daily_reward
            if vol_penalty_k > 0:
                vol = self._recent_volatility(m.condition_id)
                if vol is not None:
                    score = score / (1.0 + vol_penalty_k * vol)
            return -score

        eligible.sort(key=_rank_key)

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
        cohort_used: dict[int, float] = {}
        positive_ev_count = 0
        # FX-090: time-to-event enrichment budget + counter for this cycle.
        timing_budget = {"fetches": 0, "exhausted": False}
        timing_excluded_count = 0
        vol_excluded_count = 0  # FX-093
        event_excluded_count = 0  # FIX-1 (RC-4): event/same-day guard
        ng_excluded_count = 0  # no-gamestart discrete-event guard (held-to-resolution lever)
        sweep_excluded_count = 0  # calm-book sweep-rate filter (net-validated 2026-06-17)
        volume_excluded_count = 0  # A/B C1: max 24h volume filter
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

            # FIX-1 (RC-4): event/same-day guard (default-off via
            # RF_ALLOC_EVENT_DATE_GUARD). Catches markets whose end_date_iso is an
            # unreliable sentinel/null (e.g. the SpaceX IPO-day suite) that
            # `_timing_excluded` cannot. Reuses the same enrichment fetch above; only
            # acts on enriched candidates, so the un-enriched tail is unaffected.
            e_excluded, e_reason = self._event_guard_excluded(m)
            if e_excluded:
                m.event_guard_reason = e_reason
                m.timing_excluded_reason = e_reason  # surface in existing telemetry/feedback
                event_excluded_count += 1
                avoids.append(m)
                continue

            # No-gamestart discrete-event guard (held-to-resolution lever, verified 2026-06-17).
            ng_excluded, ng_reason = self._no_gamestart_event_excluded(m)
            if ng_excluded:
                m.event_guard_reason = ng_reason
                m.timing_excluded_reason = ng_reason
                ng_excluded_count += 1
                avoids.append(m)
                continue

            # FX-093: recent-volatility filter. Exclude candidates whose
            # book_snapshots midpoint has swung more than the cap over the
            # window — news-active markets that adversely fill our resting
            # quotes. Triggered by book movement (no fill needed); fail-open
            # when there's insufficient history (FX-051 cooldown backstops).
            vol_cap = ALLOC_MAX_RECENT_VOLATILITY()
            # A/B trader cohorts (C1/C2): apply a TIGHTER volatility gate.
            # Off (or C0) => unchanged baseline gate.
            if cfg("RF_AB_EXPERIMENT_ENABLED") and self._ab_cohort(m.condition_id) != 0:
                try:
                    trader_cap = float(cfg("RF_AB_C1_MAX_RECENT_VOLATILITY") or 0.0)
                except (TypeError, ValueError):
                    trader_cap = 0.0
                if trader_cap > 0:
                    vol_cap = trader_cap
            if vol_cap and vol_cap > 0:
                recent_vol = self._recent_volatility(m.condition_id)
                if recent_vol is not None and recent_vol > vol_cap:
                    m.timing_excluded_reason = f"volatility_{recent_vol:.3f}>{vol_cap:.2f}"
                    vol_excluded_count += 1
                    avoids.append(m)
                    continue

            # Calm-book SWEEP-RATE filter (net-validated 2026-06-17: J≈0.49 on real per-market net).
            # Exclude markets whose book is being swept (news-driven) beyond the cap. Fail-open when
            # there's insufficient book history (returns None). Disabled by default (cap 0).
            try:
                sweep_cap = float(cfg("RF_ALLOC_MAX_RECENT_SWEEP_RATE") or 0.0)
            except (TypeError, ValueError):
                sweep_cap = 0.0
            if sweep_cap > 0:
                recent_sweep = self._recent_sweep_rate(m.condition_id)
                if recent_sweep is not None and recent_sweep > sweep_cap:
                    m.timing_excluded_reason = f"sweep_{recent_sweep:.3f}>{sweep_cap:.2f}"
                    sweep_excluded_count += 1
                    avoids.append(m)
                    continue

            # A/B C2: strict 24h volume filter. C2 only trades markets where we
            # have a known volume_24h and it is below the cap. Missing/unknown
            # volume (<= 0) is excluded, matching the user's strict interpretation.
            if cfg("RF_AB_EXPERIMENT_ENABLED") and self._ab_cohort(m.condition_id) == 2:
                try:
                    volume_cap = float(cfg("RF_AB_C2_MAX_VOLUME_24H") or 0.0)
                except (TypeError, ValueError):
                    volume_cap = 0.0
                if volume_cap > 0:
                    if m.volume_24h <= 0:
                        m.timing_excluded_reason = "volume_24h_missing"
                        volume_excluded_count += 1
                        avoids.append(m)
                        continue
                    if m.volume_24h >= volume_cap:
                        m.timing_excluded_reason = f"volume_24h_{m.volume_24h:.0f}>={volume_cap:.0f}"
                        volume_excluded_count += 1
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

            # Phase 5a: per-market capital cap (skip if cap < min_size economics)
            try:
                from config import cfg
                cap_usd = float(cfg("RF_MAX_CAPITAL_PER_MARKET_USD") or 0.0)
            except Exception:
                cap_usd = 0.0
            if cap_usd > 0 and cost_per_market > cap_usd:
                capped_shares = int(cap_usd / cost_per_share)
                if capped_shares < m.min_size:
                    avoids.append(m)
                    continue
                target_shares = capped_shares
                cost_per_market = target_shares * cost_per_share

            # A/B experiment: enforce a per-cohort deployed-notional budget so
            # each cohort deploys the same target capital. This isolates the rule
            # effect from capital-allocation differences in the A/B analysis.
            cohort = self._ab_cohort(m.condition_id) if cfg("RF_AB_EXPERIMENT_ENABLED") else 0
            if cfg("RF_AB_EXPERIMENT_ENABLED"):
                try:
                    ab_budget = float(cfg("RF_AB_TOTAL_CAPITAL_USD") or 0.0)
                    cohort_count = max(1, int(cfg("RF_AB_COHORT_COUNT") or 1))
                except (TypeError, ValueError):
                    ab_budget = 0.0
                    cohort_count = 1
                if ab_budget > 0:
                    per_cohort_budget = ab_budget / cohort_count
                    if cohort_used.get(cohort, 0.0) + cost_per_market > per_cohort_budget:
                        avoids.append(m)
                        continue

            m.target_shares = target_shares
            m.target_capital = round(cost_per_market, 2)

            deploys.append(m)
            used += cost_per_market
            cohort_used[cohort] = cohort_used.get(cohort, 0.0) + cost_per_market

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
            f"timing_excluded={timing_excluded_count} event_excluded={event_excluded_count} ng_event_excluded={ng_excluded_count} vol_excluded={vol_excluded_count} sweep_excluded={sweep_excluded_count} volume_excluded={volume_excluded_count} timing_fetches={timing_budget['fetches']} "
            f"notional_total=${used:.2f} wallet=${wallet_usd:.2f} "
            f"overcommit_ratio={notional_overcommit_ratio:.2f}× "
            f"(target band 3-8× per Ground Rule 2) "
            f"p4_size_reduction_cids={len(size_reduction)} "
            f"p4_global_tighten={global_tighten} "
            f"p10_global_reward_low={global_reward_low} "
            f"p11_q_distrust_cids={len(q_distrust)}"
        )

        # A3: candidate-features survivorship log (behind RF_CANDIDATE_FEATURE_LOG_ENABLED).
        # Build the eligible-set feature vectors IN-MEMORY only — pure reads of each m, no
        # mutation, no I/O. The orchestrator (simple_oversight) writes them to the isolated
        # candidate_features.db AFTER the alloc file. Fail-open: any error -> empty list, so
        # the allocator's decision/output is NEVER affected (proven by the byte-identical test).
        candidate_feature_records: list[dict] = []
        if cfg("RF_CANDIDATE_FEATURE_LOG_ENABLED"):
            try:
                _deploy_ids = {m.condition_id for m in deploys}
                for m in eligible:
                    candidate_feature_records.append({
                        "condition_id": m.condition_id,
                        "cohort": self._ab_cohort(m.condition_id),
                        "action": "deploy" if m.condition_id in _deploy_ids else "avoid",
                        "reason": m.timing_excluded_reason or m.event_guard_reason or "",
                        "daily_rate": m.daily_rate,
                        "max_spread": m.max_spread,
                        "min_size": m.min_size,
                        "midpoint_guess": m.midpoint_guess,
                        "volume_24h": m.volume_24h,
                        "expected_q_share": m.expected_q_share,
                        "q_share_source": m.q_share_source,
                        "expected_daily_reward": m.expected_daily_reward,
                        "target_shares": m.target_shares,
                        "target_capital": m.target_capital,
                        "target_queue_usd": self._effective_target_queue_usd(m.condition_id),
                        "hours_to_resolution": self._hours_to_resolution(m.end_date_iso),
                        "end_date_iso": m.end_date_iso,
                        "game_start_time": m.game_start_time,
                        "question": m.question,
                    })
            except Exception as e:
                candidate_feature_records = []
                log.debug(f"[A3] candidate-features capture skipped (fail-open): {e}")

        return AllocationResult(
            deploys=deploys, avoids=avoids,
            total_capital=wallet_usd, capital_deployed=round(used, 2),
            expected_total_reward=round(expected_total, 4),
            kill_switch=False, kill_reason="",
            sources_used=sources_used,
            candidate_features=candidate_feature_records,
        )

    # ── Minimal safety (replaces 14-invariant SafetyController) ──

    def check_kill_switch(
        self,
        wallet_usd: float,
        portfolio_value_usd: float,
        portfolio_peak_usd: float,
        realized_loss_24h: float,
    ) -> tuple[bool, str]:
        """Two kill triggers: 24h realized loss > 10% wallet, or 15% drawdown.

        FX-095: drawdown uses cash+marked inventory (portfolio_value_usd vs peak).
        Returns (should_kill, reason).
        """
        if portfolio_value_usd <= 0:
            return True, f"portfolio collapsed to ${portfolio_value_usd:.2f}"

        if realized_loss_24h > wallet_usd * KILL_LOSS_FRAC:
            return True, (
                f"24h realized loss ${realized_loss_24h:.2f} > "
                f"{KILL_LOSS_FRAC * 100:.0f}% of wallet ${wallet_usd:.2f}"
            )

        if portfolio_peak_usd > 0:
            drawdown = 1.0 - (portfolio_value_usd / portfolio_peak_usd)
            # KILL_DRAWDOWN_FRAC (0.15) is the hardcoded default. RF_KILL_DRAWDOWN_FRAC
            # allows a RECORDED, time-bounded operator loosening (see ground_rules.md
            # change log) WITHOUT a redeploy — e.g. to ride out a stale-peak window
            # deliberately. Falsy/invalid override falls back to the 0.15 default, so
            # the override can only ever be an explicit, valid number (you can't
            # accidentally disable the kill by setting 0).
            try:
                dd_frac = float(cfg("RF_KILL_DRAWDOWN_FRAC") or KILL_DRAWDOWN_FRAC)
            except (TypeError, ValueError):
                dd_frac = KILL_DRAWDOWN_FRAC
            if drawdown > dd_frac:
                return True, (
                    f"drawdown {drawdown * 100:.1f}% > "
                    f"{dd_frac * 100:.0f}% from peak "
                    f"${portfolio_peak_usd:.2f} "
                    f"(portfolio=${portfolio_value_usd:.2f}, cash=${wallet_usd:.2f})"
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
