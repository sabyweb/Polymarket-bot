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

log = logging.getLogger("simple_allocator")


# ── Configuration (tuned against $1.2k wallet, real Polymarket q_share) ──
#
# Aggregate strategy: Polymarket's $1/day threshold is per-USER not per-market.
# We deploy on many markets at modest expected reward each, summing to clear
# the threshold. Per-market filter set deliberately low.
#
# Calibrated from agent-4's competitive analysis:
#   - Bot historical median q_share: ~0.074%
#   - At $1.2k wallet with larger orders, expected ~0.3-1.0% (model-projected)
#   - $1.2k × DEPLOY_RATIO = $1140 budget
#   - 20 markets × $60 cap = $1200 max utilization

MIN_DAILY_RATE_USD = 10.0          # market floor — below this not worth a slot
MIN_EXPECTED_PER_MARKET = 0.01     # 1¢/day floor — aggregate-strategy permissive
MAX_DEPLOYED_MARKETS = 20          # hard cap on simultaneous deploys
MAX_PER_MARKET_USD = 60.0          # per-market exposure cap (~5% of $1.2k wallet)
MIN_PER_MARKET_USD = 10.0          # minimum per-market notional (venue min_size dependent)
DEPLOY_RATIO = 0.95                # fraction of wallet deployable
COLD_START_Q_SHARE = 0.005         # 0.5% prior — matches bot historical median band

# Kill switch thresholds — these replace SafetyController's 14 invariants
KILL_LOSS_FRAC = 0.10              # halt on 24h realized loss > 10% of wallet
KILL_DRAWDOWN_FRAC = 0.15          # halt on 15% drawdown from peak wallet

# Polymarket API
CLOB_HOST = "https://clob.polymarket.com"
USER_PCTS_PATH = "/rewards/user/percentages"
USER_TOTAL_PATH = "/rewards/user/total"
MARKETS_CURRENT_PATH = "/rewards/markets/current"


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

    # Filled in by SimpleAllocator
    expected_q_share: float = 0.0
    expected_daily_reward: float = 0.0
    q_share_source: str = ""    # "api" | "cumulative" | "cold_start"
    target_shares: int = 0
    target_capital: float = 0.0


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
    ):
        self.db_path = db_path
        self.wallet_address = wallet_address
        self.funder = funder
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self._now = _now or time.time
        self._http = _http or requests.get

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
                    markets.append(
                        CandidateMarket(
                            condition_id=m["condition_id"],
                            yes_tid=m.get("yes_token_id", ""),
                            no_tid=m.get("no_token_id", ""),
                            daily_rate=float(m.get("native_daily_rate", m.get("total_daily_rate", 0)) or 0),
                            max_spread=float(m.get("rewards_max_spread", 4.5) or 4.5),
                            min_size=int(m.get("rewards_min_size", 20) or 20),
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
        return COLD_START_Q_SHARE, "cold_start"

    # ── Allocation logic ──

    def _est_cost_per_market(self, m: CandidateMarket) -> float:
        """Estimate USD cost for full deploy (both sides) at min_size.

        cost ≈ min_size × cost_per_share × 2_sides
        cost_per_share at midpoint 0.5 = 0.5 × 2 = 1.0 (sum); use midpoint guess.
        Conservative: use 0.5 unless we know better.
        """
        # Both sides each cost ~midpoint per share; total ≈ 2 × midpoint × shares
        # but the actual cost is min(midpoint, 1-midpoint) for buying each side
        # Most reward markets sit near 0.5; if midpoint_guess=0.5, cost ~= 1.0 × shares
        cost_per_share = max(0.10, min(m.midpoint_guess, 1.0 - m.midpoint_guess) * 2.0)
        return m.min_size * cost_per_share

    def compute(
        self,
        wallet_usd: float,
        wallet_peak_usd: float,
        wallet_24h_ago_usd: Optional[float],
        realized_loss_24h: float,
        markets: Optional[list[CandidateMarket]] = None,
    ) -> AllocationResult:
        """Main allocation entry point.

        Args:
            wallet_usd: current wallet balance
            wallet_peak_usd: highest wallet observed (for drawdown calc)
            wallet_24h_ago_usd: wallet 24h ago (or None if unknown)
            realized_loss_24h: sum of negative pnl from unwinds in last 24h (positive USD)
            markets: optional override (for testing); if None, fetches from Polymarket

        Returns AllocationResult with deploys, avoids, and kill signal.
        """
        sources_used = {"api": 0, "cumulative": 0, "cold_start": 0}

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

        for m in candidates:
            q, src = self.estimate_q_share(m.condition_id, api_shares, cumulative)
            m.expected_q_share = q
            m.expected_daily_reward = m.daily_rate * q
            m.q_share_source = src
            sources_used[src] += 1

        # ── Filter ──
        eligible = [
            m for m in candidates
            if m.daily_rate >= MIN_DAILY_RATE_USD
            and m.expected_daily_reward >= MIN_EXPECTED_PER_MARKET
            and m.yes_tid and m.no_tid
        ]

        # ── Rank by expected reward ──
        eligible.sort(key=lambda x: -x.expected_daily_reward)

        # ── Budget allocation ──
        budget = wallet_usd * DEPLOY_RATIO
        deploys: list[CandidateMarket] = []
        avoids: list[CandidateMarket] = []
        used = 0.0
        for m in eligible:
            if len(deploys) >= MAX_DEPLOYED_MARKETS:
                avoids.append(m)
                continue

            # Size to per-market cap
            cost_per_market = max(MIN_PER_MARKET_USD,
                                  min(MAX_PER_MARKET_USD, self._est_cost_per_market(m)))
            if used + cost_per_market > budget:
                avoids.append(m)
                continue

            # Compute shares from target capital
            cost_per_share = max(0.10, min(m.midpoint_guess, 1.0 - m.midpoint_guess) * 2.0)
            target_shares = max(m.min_size, int(cost_per_market / cost_per_share))
            m.target_shares = target_shares
            m.target_capital = round(cost_per_market, 2)

            deploys.append(m)
            used += cost_per_market

        # Include non-eligible candidates as avoids (for telemetry / farmer visibility)
        non_eligible = [m for m in candidates if m not in eligible]
        avoids.extend(non_eligible)

        expected_total = sum(m.expected_daily_reward for m in deploys)

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
                "end_date_iso": "",           # farmer fetches from CLOB if missing
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

        payload = {
            "version": "simple-1.0",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "num_deploy": len(result.deploys),
            "num_avoid": len(result.avoids),
            "total_capital_deployed": result.capital_deployed,
            "total_capital": round(result.total_capital, 2),
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
