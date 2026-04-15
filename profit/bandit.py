"""Thompson-Sampling Bandit — per-market exploration bonus.

Each market has Beta(alpha, beta) belief over "this market produces positive
PnL". alpha increments on a successful 24h cycle (reward > fill_damage),
beta increments otherwise. `sample()` draws from the posterior — winners
are exploited, losers keep a small exploration chance.

Invariants:
  1. sampled score is clamped to >= MIN_SCORE (0.3) so a cold/losing market
     never collapses to zero allocation — the allocator decides avoidance
     via RAS, not by bandit starvation.
  2. update() uses real PnL only (reward - fill_damage over last 24h).
     No synthetic signals, no leaks from the scorer.
  3. Default prior (no data) is Beta(1, 1) → uniform → sample mean 0.5,
     clamped to MIN_SCORE if the draw is below 0.3.
  4. Never raises. All DB failures log-and-skip.
  5. Deterministic except for the numpy.random.beta draw in sample().
"""

import logging
import time

import numpy as np

from oversight.data_collector import _connect_db

log = logging.getLogger("profit.bandit")

# STEP 2: Clamp floor. A losing market still gets a small chance so the
# bandit can re-learn if conditions change.
MIN_SCORE = 0.3

# STEP 3: PnL lookback window (24h per spec)
PNL_WINDOW_SECS = 86400

# STEP 12 invariant 1: beta-distribution parameters must stay positive.
# Defensive floor in case someone pokes malformed rows into the DB.
MIN_BETA_PARAM = 1e-6


class Bandit:
    """Thompson-Sampling bandit over per-market posteriors.

    Lazy-creates the `bandit_state` table. Safe to instantiate every cycle.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_table()

    # ── Storage ───────────────────────────────────────────────────

    def _ensure_table(self) -> None:
        """STEP 1: Create bandit_state table if missing."""
        try:
            db = _connect_db(self.db_path)
            db.execute(
                """CREATE TABLE IF NOT EXISTS bandit_state (
                    market_id         TEXT PRIMARY KEY,
                    alpha             REAL NOT NULL,
                    beta              REAL NOT NULL,
                    last_updated_ts   INTEGER NOT NULL
                )"""
            )
            db.commit()
            db.close()
        except Exception as e:
            # Invariant 5: never crash — degrade to pure uniform priors.
            log.warning(f"bandit_state table init failed: {e}")

    def load_state(self) -> dict[str, tuple[float, float]]:
        """Return {market_id: (alpha, beta)}. Empty on any failure."""
        try:
            db = _connect_db(self.db_path)
            rows = db.execute(
                "SELECT market_id, alpha, beta FROM bandit_state"
            ).fetchall()
            db.close()
        except Exception as e:
            log.warning(f"bandit load_state failed: {e}")
            return {}

        out: dict[str, tuple[float, float]] = {}
        for r in rows:
            mid = r["market_id"] if hasattr(r, "keys") else r[0]
            a = r["alpha"] if hasattr(r, "keys") else r[1]
            b = r["beta"] if hasattr(r, "keys") else r[2]
            try:
                a = max(MIN_BETA_PARAM, float(a))
                b = max(MIN_BETA_PARAM, float(b))
            except (TypeError, ValueError):
                continue
            out[mid] = (a, b)
        return out

    def _persist(self, updates: dict[str, tuple[float, float]]) -> None:
        """Upsert (alpha, beta) for every market in `updates`."""
        if not updates:
            return
        now_ts = int(time.time())
        try:
            db = _connect_db(self.db_path)
            db.executemany(
                "INSERT INTO bandit_state (market_id, alpha, beta, last_updated_ts) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(market_id) DO UPDATE SET "
                "alpha=excluded.alpha, beta=excluded.beta, "
                "last_updated_ts=excluded.last_updated_ts",
                [(mid, a, b, now_ts) for mid, (a, b) in updates.items()],
            )
            db.commit()
            db.close()
        except Exception as e:
            log.warning(f"bandit persist failed: {e}")

    # ── STEP 3: Update ────────────────────────────────────────────

    def update(self) -> dict:
        """Recompute posteriors from last 24h of real PnL.

        success := (reward - fill_damage) > 0, per market.
            reward      = sum(reward_earned_est) from unwinds in window
                          + sum(reward_rate_hr * hours_held) from fills
                          (unwinds carry the estimated reward attribution
                          that reward_farmer writes at sell time)
            fill_damage = sum(shares * clob_cost)_fills
                          - sum(usd_value)_unwinds
                          (i.e. gross fill cost net of dump revenue)

        On success → alpha += 1. On loss → beta += 1.

        Returns a summary dict: {n_markets, n_success, n_loss, status}.
        """
        cutoff = time.time() - PNL_WINDOW_SECS

        # Gather per-market reward attribution and fill damage.
        rewards: dict[str, float] = {}
        fill_costs: dict[str, float] = {}
        dump_revenues: dict[str, float] = {}
        try:
            db = _connect_db(self.db_path)

            # Reward component 1: unwind-attributed rewards (bot's best estimate
            # at time of sell). This is the only per-market reward signal we
            # have because Polymarket's /rewards/earned endpoint is aggregate.
            for r in db.execute(
                "SELECT condition_id, SUM(reward_earned_est) as rew "
                "FROM unwinds WHERE ts > ? "
                "GROUP BY condition_id",
                (cutoff,),
            ).fetchall():
                cid = r[0]
                rewards[cid] = rewards.get(cid, 0.0) + (r[1] or 0.0)

            # Fill damage: gross cost of fills
            for r in db.execute(
                "SELECT condition_id, SUM(shares * clob_cost) as cost "
                "FROM fills WHERE ts > ? "
                "GROUP BY condition_id",
                (cutoff,),
            ).fetchall():
                fill_costs[r[0]] = r[1] or 0.0

            # Dump revenue: offset against fill damage
            for r in db.execute(
                "SELECT condition_id, SUM(usd_value) as rev "
                "FROM unwinds WHERE ts > ? "
                "GROUP BY condition_id",
                (cutoff,),
            ).fetchall():
                dump_revenues[r[0]] = r[1] or 0.0

            db.close()
        except Exception as e:
            log.warning(f"bandit update query failed: {e}")
            return {"status": "query_failed", "error": str(e)}

        # All markets touched by fills or unwinds in the window
        cids = set(rewards) | set(fill_costs) | set(dump_revenues)
        if not cids:
            return {"status": "no_data", "n_markets": 0}

        # Load current posteriors, update, persist.
        state = self.load_state()
        updates: dict[str, tuple[float, float]] = {}
        n_success = 0
        n_loss = 0

        for cid in cids:
            reward = rewards.get(cid, 0.0)
            fill_cost = fill_costs.get(cid, 0.0)
            dump_rev = dump_revenues.get(cid, 0.0)
            net_damage = max(0.0, fill_cost - dump_rev)
            pnl = reward - net_damage

            alpha, beta = state.get(cid, (1.0, 1.0))
            if pnl > 0:
                alpha += 1.0
                n_success += 1
            else:
                beta += 1.0
                n_loss += 1

            # Invariant: keep parameters strictly positive
            alpha = max(MIN_BETA_PARAM, alpha)
            beta = max(MIN_BETA_PARAM, beta)
            updates[cid] = (alpha, beta)

        self._persist(updates)
        log.info(
            f"[BANDIT] updated {len(updates)} markets "
            f"(success={n_success}, loss={n_loss})"
        )
        return {
            "status": "ok",
            "n_markets": len(updates),
            "n_success": n_success,
            "n_loss": n_loss,
        }

    # ── STEP 4: Sample ────────────────────────────────────────────

    def sample(self) -> dict[str, float]:
        """Draw Thompson samples from every known market's posterior.

        Returns {market_id: score} with score = max(MIN_SCORE, beta_draw).

        Markets not in the table fall back to the uniform prior implicitly
        through the allocator's `.get(cid, 1.0)` lookup — we don't need to
        enumerate them here (callers don't have a global market list at
        this layer).
        """
        state = self.load_state()
        if not state:
            return {}

        # PART 9: seed numpy from the hash of the current cycle timestamp
        # (seconds-precision). A hash of the cycle id is reproducible from
        # the recorded timestamp alone — no nanosecond drift, no per-call
        # randomness from system clock jitter.
        cycle_id = int(time.time())
        cycle_id_hash = hash(cycle_id) & 0xFFFFFFFF
        np.random.seed(cycle_id_hash)

        out: dict[str, float] = {}
        for cid, (a, b) in state.items():
            try:
                draw = float(np.random.beta(a, b))
            except Exception as e:
                # Invariant 5 — degrade to neutral score rather than crash
                log.warning(f"bandit sample failed for {cid}: {e}")
                draw = 0.5
            out[cid] = max(MIN_SCORE, draw)
        return out
