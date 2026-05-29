"""FX-049: Wallet-invariant reconciliation — defense-in-depth backstop.

Contracts (R6) — see module docstring of oversight/wallet_reconciliation.py
for full statement.

C1: First call with empty history → 'baseline' row written, returns
    'no_baseline'. No alert. (Genuine first-run; nothing to compare.)

C2: Subsequent call with |actual_delta − expected_delta| ≤ threshold
    → 'ok' row, returns 'ok'. No CRITICAL.

C3: Subsequent call with |actual_delta − expected_delta| > threshold
    → 'desync' row, log.critical("WALLET_DESYNC: ...") emitted.

C4: Reward-fetch exception → 'fail_open' row, log.warning, no CRITICAL.

C5: Every call advances the baseline to (now, actual_wallet_now) for
    the next cycle (incremental, not cumulative-from-genesis).

C6: Reward-fetch returns 0 with ok=True (no events) → reconciliation
    proceeds normally with rewards_delta=0.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oversight.wallet_reconciliation import reconcile_wallet_invariant


@pytest.fixture(autouse=True)
def _mock_discord_send(monkeypatch):
    """FX-074: neutralize the real Discord POST for every test in this module.

    After FX-074 a desync fires ``alert_wallet_desync`` -> ``alerts._send_discord``,
    and ``.env`` carries a live ``DISCORD_WEBHOOK_URL`` (captured into alerts at
    import via ``from config import DISCORD_WEBHOOK_URL``). Without this, the
    existing desync-path tests would each POST to the operator's Discord.
    Patching ``alerts._send_discord`` is the correct interception point.
    """
    import alerts
    monkeypatch.setattr(alerts, "_send_discord", lambda *a, **k: None)
    yield


class _FakeDB:
    """In-memory stand-in for BotDatabase. Records every insert and
    serves the most-recent row on load. Lets tests inject precise
    `fills`/`unwinds` since-window sums without spinning up SQLite.
    """
    def __init__(self, fills_delta: float = 0.0, unwinds_delta: float = 0.0):
        self.history: list[dict] = []
        self._fills_delta = fills_delta
        self._unwinds_delta = unwinds_delta

    def load_latest_wallet_reconcile(self) -> dict | None:
        return dict(self.history[-1]) if self.history else None

    def insert_wallet_reconcile(self, **kwargs) -> None:
        # Mirror the production schema's column ordering / ts field.
        row = {
            "ts": kwargs.get("baseline_ts"),  # row's ts == new baseline_ts
            **kwargs,
        }
        # Production uses time.time() for ts; tests pass _now_fn via the
        # reconcile_fn so the row's ts matches baseline_ts (the canonical
        # row that the NEXT call will use as its baseline_ts).
        self.history.append(row)

    def sum_fills_usd_since(self, since_ts: float) -> float:
        return self._fills_delta

    def sum_unwinds_usd_since(self, since_ts: float) -> float:
        return self._unwinds_delta


def _frozen_now(t: float):
    return lambda: t


class TestReconcileFirstRunBaseline(unittest.TestCase):
    """C1: empty history → baseline snapshot, no alert."""

    def test_first_run_writes_baseline_no_alert(self):
        db = _FakeDB()
        fetch = MagicMock(return_value=(0.0, True))
        result = reconcile_wallet_invariant(
            db, actual_wallet_now=227.43, funder="0xFAKE",
            threshold_usd=0.50,
            _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779600000.0),
        )
        self.assertEqual(result["status"], "no_baseline")
        self.assertEqual(result["divergence"], 0.0)
        self.assertEqual(len(db.history), 1)
        self.assertEqual(db.history[0]["status"], "baseline")
        # data-api should NOT be hit on first run (nothing to compare yet)
        fetch.assert_not_called()


class TestReconcileWithinTolerance(unittest.TestCase):
    """C2: |divergence| ≤ threshold → 'ok', no CRITICAL."""

    def test_zero_divergence_returns_ok(self):
        # Baseline row already exists.
        db = _FakeDB(fills_delta=10.0, unwinds_delta=10.0)
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 200.0, "expected_wallet": 200.0,
            "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 200.0,
        })
        # Window: bot recorded +10 unwinds, -10 fills, +0 rewards = 0 delta.
        # Actual wallet unchanged = 200. → divergence 0.
        fetch = MagicMock(return_value=(0.0, True))
        result = reconcile_wallet_invariant(
            db, actual_wallet_now=200.0, funder="0xFAKE",
            threshold_usd=0.50,
            _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
        )
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["divergence"], 0.0, places=4)
        self.assertEqual(db.history[-1]["status"], "ok")

    def test_small_fee_drift_still_ok_under_threshold(self):
        """Pre-FX-050 fee drift was ~$0.34/dump. Threshold $0.50 absorbs single events."""
        db = _FakeDB(fills_delta=40.0, unwinds_delta=39.0)
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 227.43,
            "expected_wallet": 227.43, "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 227.43,
        })
        # Bot believes delta = unwinds 39 - fills 40 + rewards 0 = -$1.
        # Actual wallet: 227.43 - 1.34 = 226.09 → actual_delta = -$1.34.
        # Divergence: -1.34 - (-1.0) = -$0.34 (within $0.50 threshold).
        fetch = MagicMock(return_value=(0.0, True))
        result = reconcile_wallet_invariant(
            db, actual_wallet_now=226.09, funder="0xFAKE",
            threshold_usd=0.50,
            _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
        )
        self.assertEqual(result["status"], "ok",
                         f"$0.34 fee drift should be within $0.50 threshold. Got: {result}")
        self.assertAlmostEqual(result["divergence"], -0.34, places=2)


class TestReconcileDivergenceAlerts(unittest.TestCase):
    """C3: divergence > threshold → 'desync' + CRITICAL log."""

    def test_large_divergence_emits_critical(self):
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 227.43,
            "expected_wallet": 227.43, "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 227.43,
        })
        # Bot: no trades, no rewards. Expected: wallet unchanged.
        # Actual: wallet jumped +$10 (could be missing reward event or
        # external deposit). Divergence > $0.50 threshold → desync.
        fetch = MagicMock(return_value=(0.0, True))
        with self.assertLogs("oversight.wallet_reconciliation", level="CRITICAL") as cap:
            result = reconcile_wallet_invariant(
                db, actual_wallet_now=237.43, funder="0xFAKE",
                threshold_usd=0.50,
                _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
            )
        self.assertEqual(result["status"], "desync")
        self.assertAlmostEqual(result["divergence"], 10.0, places=4)
        # CRITICAL log line content
        self.assertTrue(
            any("WALLET_DESYNC" in line for line in cap.output),
            f"Expected 'WALLET_DESYNC' in CRITICAL log, got: {cap.output}",
        )

    def test_negative_divergence_also_alerts(self):
        """Bot thinks it has more than it does → also desync."""
        db = _FakeDB(fills_delta=0.0, unwinds_delta=10.0)  # bot recorded +$10 unwind
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 200.0,
            "expected_wallet": 200.0, "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 200.0,
        })
        # Bot expects $200 + $10 = $210. Actual: $200 (the unwind was phantom).
        # Divergence: 200 - 210 = -$10 → desync.
        fetch = MagicMock(return_value=(0.0, True))
        with self.assertLogs("oversight.wallet_reconciliation", level="CRITICAL"):
            result = reconcile_wallet_invariant(
                db, actual_wallet_now=200.0, funder="0xFAKE",
                threshold_usd=0.50,
                _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
            )
        self.assertEqual(result["status"], "desync")
        self.assertAlmostEqual(result["divergence"], -10.0, places=4)


class TestReconcileFailOpen(unittest.TestCase):
    """C4: reward-fetch failure → 'fail_open', no CRITICAL."""

    def test_reward_fetch_failure_writes_fail_open_row(self):
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 200.0,
            "expected_wallet": 200.0, "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 200.0,
        })
        fetch = MagicMock(return_value=(0.0, False))  # ok=False signals failure
        with self.assertLogs("oversight.wallet_reconciliation", level="WARNING") as cap:
            result = reconcile_wallet_invariant(
                db, actual_wallet_now=200.0, funder="0xFAKE",
                threshold_usd=0.50,
                _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
            )
        self.assertEqual(result["status"], "fail_open")
        self.assertFalse(result["rewards_fetch_ok"])
        # Must NOT emit CRITICAL (fail-open is silent on divergence)
        for line in cap.output:
            self.assertNotIn("CRITICAL", line,
                             "fail_open must not emit CRITICAL")
        # But warning IS emitted for operator visibility
        self.assertTrue(
            any("fail_open" in line.lower() for line in cap.output),
            f"Expected 'fail_open' in warning log, got: {cap.output}",
        )


class TestReconcileBaselineAdvancement(unittest.TestCase):
    """C5: every call advances the baseline."""

    def test_baseline_advances_after_ok_cycle(self):
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 200.0,
            "expected_wallet": 200.0, "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 200.0,
        })
        fetch = MagicMock(return_value=(0.0, True))
        reconcile_wallet_invariant(
            db, actual_wallet_now=200.0, funder="0xFAKE", threshold_usd=0.50,
            _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
        )
        # New row written with the post-cycle ts as the next baseline_ts
        self.assertEqual(len(db.history), 2)
        self.assertEqual(db.history[-1]["baseline_ts"], 1779603600.0)
        self.assertEqual(db.history[-1]["baseline_wallet"], 200.0)

    def test_baseline_advances_after_desync_cycle(self):
        """Even on desync, the baseline must advance so the next cycle
        measures the NEW window (avoid double-counting the same divergence)."""
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 200.0,
            "expected_wallet": 200.0, "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 200.0,
        })
        fetch = MagicMock(return_value=(0.0, True))
        with self.assertLogs("oversight.wallet_reconciliation", level="CRITICAL"):
            reconcile_wallet_invariant(
                db, actual_wallet_now=220.0, funder="0xFAKE",  # +$20 unexplained
                threshold_usd=0.50,
                _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
            )
        # The desync row should now be the baseline for the NEXT call
        self.assertEqual(db.history[-1]["status"], "desync")
        self.assertEqual(db.history[-1]["baseline_wallet"], 220.0)


class TestReconcileWithRewards(unittest.TestCase):
    """C6: rewards inflow correctly attributed (no false-positive divergence)."""

    def test_reward_inflow_explains_wallet_growth(self):
        """Polymarket pays a $5 REWARD; bot's wallet grows by $5;
        reconciler attributes correctly → no divergence.
        """
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 227.43,
            "expected_wallet": 227.43, "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 227.43,
        })
        # No trades, but $5 reward arrived in window
        fetch = MagicMock(return_value=(5.0, True))
        result = reconcile_wallet_invariant(
            db, actual_wallet_now=232.43, funder="0xFAKE",
            threshold_usd=0.50,
            _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
        )
        # Expected delta = unwinds(0) - fills(0) + rewards(5) = $5
        # Actual delta = 232.43 - 227.43 = $5. Divergence = 0.
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["divergence"], 0.0, places=4)

    def test_unexplained_inflow_without_reward_flags_desync(self):
        """If wallet grew but data-api shows no reward → desync (operator
        deposit? on-chain glitch? Polymarket reporting lag? Investigate)."""
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        db.history.append({
            "ts": 1779600000.0, "actual_wallet": 227.43,
            "expected_wallet": 227.43, "divergence": 0.0, "status": "baseline",
            "baseline_ts": 1779600000.0, "baseline_wallet": 227.43,
        })
        fetch = MagicMock(return_value=(0.0, True))  # ok=True, but no rewards found
        with self.assertLogs("oversight.wallet_reconciliation", level="CRITICAL"):
            result = reconcile_wallet_invariant(
                db, actual_wallet_now=237.43, funder="0xFAKE",  # +$10
                threshold_usd=0.50,
                _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
            )
        self.assertEqual(result["status"], "desync")


class TestReconcileFX074ExternalAlert(unittest.TestCase):
    """FX-074: a [CRITICAL] WALLET_DESYNC now PAGES via the external alert
    channel (alert_wallet_desync), while staying OBSERVATIONAL — it never
    halts or gates allocation. These lock the alert to the desync branch only
    and prove the wiring is fail-open (an alert-send failure must not crash
    the reconciler or block trading).
    """

    def _seed_baseline(self, db, wallet, ts=1779600000.0):
        db.history.append({
            "ts": ts, "actual_wallet": wallet, "expected_wallet": wallet,
            "divergence": 0.0, "status": "baseline",
            "baseline_ts": ts, "baseline_wallet": wallet,
        })

    def test_desync_fires_external_alert(self):
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        self._seed_baseline(db, 227.43)
        fetch = MagicMock(return_value=(0.0, True))
        with patch("alerts.alert_wallet_desync") as m_alert:
            result = reconcile_wallet_invariant(
                db, actual_wallet_now=237.43, funder="0xFAKE",  # +$10 desync
                threshold_usd=0.50,
                _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
            )
        self.assertEqual(result["status"], "desync")
        m_alert.assert_called_once()
        kwargs = m_alert.call_args.kwargs
        self.assertAlmostEqual(kwargs["divergence"], 10.0, places=4)
        self.assertEqual(kwargs["threshold_usd"], 0.50)
        self.assertAlmostEqual(kwargs["actual_wallet"], 237.43, places=4)

    def test_ok_does_not_fire_external_alert(self):
        db = _FakeDB(fills_delta=10.0, unwinds_delta=10.0)  # net 0 expected
        self._seed_baseline(db, 200.0)
        fetch = MagicMock(return_value=(0.0, True))
        with patch("alerts.alert_wallet_desync") as m_alert:
            result = reconcile_wallet_invariant(
                db, actual_wallet_now=200.0, funder="0xFAKE",
                threshold_usd=0.50,
                _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
            )
        self.assertEqual(result["status"], "ok")
        m_alert.assert_not_called()

    def test_fail_open_does_not_fire_external_alert(self):
        # reward fetch failed → fail_open branch precedes the desync check;
        # even a large wallet jump must NOT page (could be unseen rewards).
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        self._seed_baseline(db, 200.0)
        fetch = MagicMock(return_value=(0.0, False))
        with patch("alerts.alert_wallet_desync") as m_alert:
            result = reconcile_wallet_invariant(
                db, actual_wallet_now=999.0, funder="0xFAKE",
                threshold_usd=0.50,
                _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
            )
        self.assertEqual(result["status"], "fail_open")
        m_alert.assert_not_called()

    def test_alert_send_failure_does_not_crash_reconciler(self):
        # The alert is wrapped in a fail-open try/except: a Discord/alert
        # failure must not propagate and must NOT skip the DB row write.
        db = _FakeDB(fills_delta=0.0, unwinds_delta=0.0)
        self._seed_baseline(db, 227.43)
        fetch = MagicMock(return_value=(0.0, True))
        with patch("alerts.alert_wallet_desync",
                   side_effect=RuntimeError("discord down")):
            with self.assertLogs(
                "oversight.wallet_reconciliation", level="WARNING"
            ) as cap:
                result = reconcile_wallet_invariant(
                    db, actual_wallet_now=237.43, funder="0xFAKE",
                    threshold_usd=0.50,
                    _fetch_rewards_fn=fetch, _now_fn=_frozen_now(1779603600.0),
                )
        self.assertEqual(result["status"], "desync")
        # Row still written AFTER the failed alert (insert runs post-branch).
        self.assertEqual(db.history[-1]["status"], "desync")
        self.assertTrue(
            any("desync alert send failed" in line for line in cap.output),
            f"Expected fail-open warning, got: {cap.output}",
        )


if __name__ == "__main__":
    unittest.main()
