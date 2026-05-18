"""Tests for the 3-layer sports market protection.

Layer 1 (Agent): market_scorer hard-avoids sports < 4h or missing end_date
Layer 2 (Bot): order_lifecycle blocks placement for sports < 4h or missing end_date
Layer 3 (Bot): reward_farmer pre-cycle sweep cancels orders on markets expiring < 1h
"""

import os
import sys
import time
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import MarketState, OrderSlot
from config import SPORTS_KEYWORDS


# ── py_clob_client_v2 mock (not installed in test env) ──────────────────────────
def _ensure_clob_types_mock():
    """Mock py_clob_client_v2 so order_lifecycle can import OrderArgs/BUY."""
    if "py_clob_client_v2" not in sys.modules:
        mock_clob = MagicMock()
        sys.modules["py_clob_client_v2"] = mock_clob
        sys.modules["py_clob_client_v2.clob_types"] = mock_clob.clob_types
        sys.modules["py_clob_client_v2.order_builder"] = mock_clob.order_builder
        sys.modules["py_clob_client_v2.order_builder.constants"] = mock_clob.order_builder.constants
        # BUY constant needs to be a string
        mock_clob.order_builder.constants.BUY = "BUY"
        mock_clob.order_builder.constants.SELL = "SELL"

_ensure_clob_types_mock()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ms(cid="cid_001", question="Test market?", end_date_iso="",
             agent_approved=True, agent_shares=50):
    """Create a MarketState for testing."""
    return MarketState(
        cid=cid, question=question, yes_tid="ytid", no_tid="ntid",
        daily_rate=20.0, max_spread=0.10, min_size=10, tick_size=0.01,
        yes_price=0.50, agent_shares=agent_shares, agent_approved=agent_approved,
        end_date_iso=end_date_iso,
    )


def _hours_from_now(hours: float) -> str:
    """Return ISO date string for N hours from now."""
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


def _make_market_metrics(**kwargs):
    """Create a MarketMetrics-like object for scorer tests."""
    from oversight.data_collector import MarketMetrics
    defaults = dict(
        condition_id="cid_001",
        question="Test market?",
        daily_rate=50.0,
        actual_reward_total=10.0,
        fill_cost_recent=0.0,
        dump_revenue_recent=0.0,
        fill_count_recent=0,
        net_pnl_recent=10.0,
        current_position_usd=0.0,
        on_book_hours=24.0,
        q_share_pct=0.05,
        end_date_iso="",
        min_size=50.0,
        max_spread=0.045,
    )
    defaults.update(kwargs)
    return MarketMetrics(**defaults)


def _make_lifecycle(markets_dict):
    """Create an OrderLifecycle with minimal mocks.

    DB mock is configured with ``is_unliquidatable -> False`` so the new
    FX-007 gate doesn't short-circuit the sports-protection paths.
    """
    from order_lifecycle import OrderLifecycle
    positions = MagicMock()
    positions.get_shares.return_value = 0
    positions.can_quote.return_value = True
    db = MagicMock()
    db.is_unliquidatable.return_value = False
    ol = OrderLifecycle(
        client=MagicMock(), db=db, positions=positions,
        rewards=MagicMock(), markets=markets_dict, dry_run=True,
    )
    ol.capital_ceiling = None
    return ol


# ═══════════════════════════════════════════════════════════════════════
# Layer 1: Agent (market_scorer)
# ═══════════════════════════════════════════════════════════════════════

class TestLayer1AgentSportsAvoid(unittest.TestCase):
    """market_scorer.classify_market() hard-avoids dangerous sports markets."""

    def test_sports_no_end_date_avoided(self):
        """Sports market with no end_date → AVOID (default-deny)."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Lakers vs Celtics — will Lakers win?",
            end_date_iso="",  # No date
        )
        s = score_market(m, hours=24)
        sm = classify_market(m, s)
        self.assertEqual(sm.action, "avoid")
        self.assertIn("no expiry", sm.reason.lower())

    def test_sports_expiring_in_2h_avoided(self):
        """Sports market expiring in 2h (< 4h block) → AVOID."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Man City vs Arsenal — Premier League winner?",
            end_date_iso=_hours_from_now(2.0),
        )
        s = score_market(m, hours=24)
        sm = classify_market(m, s)
        self.assertEqual(sm.action, "avoid")
        self.assertIn("sports market expiring", sm.reason.lower())

    def test_sports_expiring_in_48h_deployed(self):
        """Sports market expiring in 48h (> 4h) → DEPLOY (capped to min_size)."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Lakers vs Celtics — will Lakers win?",
            end_date_iso=_hours_from_now(48.0),
            daily_rate=50.0,
        )
        s = score_market(m, hours=24)
        sm = classify_market(m, s)
        self.assertEqual(sm.action, "deploy")
        # Should be capped to min_size since < 72h
        self.assertLessEqual(sm.recommended_shares, int(m.min_size))

    def test_sports_expiring_in_100h_deployed_full_size(self):
        """Sports market expiring in 100h (> 72h) → DEPLOY at full size."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Lakers vs Celtics — will Lakers win?",
            end_date_iso=_hours_from_now(100.0),
            daily_rate=50.0,
        )
        s = score_market(m, hours=24)
        sm = classify_market(m, s)
        self.assertEqual(sm.action, "deploy")

    def test_non_sports_no_end_date_deployed(self):
        """Non-sports market with no end_date → still DEPLOY (only sports default-deny)."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Will Bitcoin reach $200k by 2026?",
            end_date_iso="",
        )
        s = score_market(m, hours=24)
        sm = classify_market(m, s)
        self.assertEqual(sm.action, "deploy")

    def test_sports_keyword_coverage(self):
        """All major sports keywords trigger detection."""
        from oversight.market_scorer import classify_market, score_market
        test_questions = [
            "Team A vs Team B — who wins?",
            "Will NBA Finals winner be Lakers 2026?",
            "Will NFL Super Bowl champion be Chiefs?",
            "Will IPL cricket — Mumbai win?",
            "Will UFC 400 main event winner be Jones?",
            "Formula 1 Grand Prix — will Hamilton win?",
            "March Madness bracket winner?",
            "Premier League top scorer?",
        ]
        for q in test_questions:
            m = _make_market_metrics(
                question=q,
                end_date_iso="",  # No date → should avoid
            )
            s = score_market(m, hours=24)
            sm = classify_market(m, s)
            self.assertEqual(
                sm.action, "avoid",
                f"Expected AVOID for '{q}' (sports + no date), got {sm.action}: {sm.reason}"
            )

    def test_sports_bad_date_avoided(self):
        """Sports market with unparseable end_date → AVOID."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Lakers vs Celtics — will Lakers win?",
            end_date_iso="not-a-date",
        )
        s = score_market(m, hours=24)
        sm = classify_market(m, s)
        self.assertEqual(sm.action, "avoid")
        self.assertIn("unparseable", sm.reason.lower())


# ═══════════════════════════════════════════════════════════════════════
# Layer 1 (addendum): game_start_time Phase 1 block
# ═══════════════════════════════════════════════════════════════════════

class TestGameStartTimeBlock(unittest.TestCase):
    """Phase 1 of sports protection: block within RF_GAME_BLOCK_HOURS of kickoff.

    The game_start_time check runs BEFORE the end_date_iso gates and is
    additive — it can only make protection stricter, never looser. When
    game_start_time is absent (Gamma-routed markets), behavior is identical
    to the pre-Phase-1 world.
    """

    def test_inplay_market_avoided(self):
        """Game already started (game_start_time in the past) → AVOID via Phase 1."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Lakers vs Celtics — will Lakers win?",
            game_start_time=_hours_from_now(-0.5),   # kicked off 30min ago
            end_date_iso=_hours_from_now(4.0),        # resolves in 4h — end_date gate won't fire
        )
        sm = classify_market(m, score_market(m, hours=24))
        self.assertEqual(sm.action, "avoid")
        self.assertIn("from kickoff", sm.reason.lower())

    def test_pre_kickoff_within_block_avoided(self):
        """Kickoff in 30min → AVOID via Phase 1 (within RF_GAME_BLOCK_HOURS=1h)."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Man City vs Arsenal — Premier League winner?",
            game_start_time=_hours_from_now(0.5),
            end_date_iso=_hours_from_now(5.0),        # safe by end_date_iso alone
        )
        sm = classify_market(m, score_market(m, hours=24))
        self.assertEqual(sm.action, "avoid")
        self.assertIn("from kickoff", sm.reason.lower())

    def test_pre_kickoff_beyond_block_deployed(self):
        """Kickoff in 5h, resolves in 10h → both signals safe → DEPLOY."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Lakers vs Celtics — will Lakers win?",
            daily_rate=50.0,
            game_start_time=_hours_from_now(5.0),
            end_date_iso=_hours_from_now(10.0),
        )
        sm = classify_market(m, score_market(m, hours=24))
        self.assertEqual(sm.action, "deploy")

    def test_end_date_still_blocks_when_game_start_safe(self):
        """Kickoff in 20h (safe) but end_date in 2h (within 4h block) → AVOID
        via existing end_date_iso gate, NOT Phase 1. Confirms Phase 2-4
        still runs when Phase 1 doesn't trigger."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="NFL Sunday: Patriots vs Bills",
            game_start_time=_hours_from_now(20.0),    # far in future
            end_date_iso=_hours_from_now(2.0),         # within 4h block
        )
        sm = classify_market(m, score_market(m, hours=24))
        self.assertEqual(sm.action, "avoid")
        # Must be the end_date reason, not the kickoff reason.
        self.assertIn("expiring", sm.reason.lower())
        self.assertNotIn("from kickoff", sm.reason.lower())

    def test_missing_game_start_time_preserves_today_behavior(self):
        """Gamma-routed market (game_start_time="") with safe end_date → DEPLOY.
        Regression guard: the new Phase 1 gate MUST NOT fire on empty strings."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="UFC 300: Main Event",
            daily_rate=50.0,
            game_start_time="",                        # empty — Gamma-routed case
            end_date_iso=_hours_from_now(10.0),         # safe
            on_book_hours=24.0,                         # high confidence
        )
        sm = classify_market(m, score_market(m, hours=24))
        self.assertEqual(sm.action, "deploy")

    def test_unparseable_game_start_time_falls_through(self):
        """Garbage in game_start_time → Phase 1 silently falls through, then
        Phase 3 (end_date_iso ≤ 4h) fires. We do NOT avoid on parse error
        because end_date_iso is still a valid signal."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="NBA: Lakers vs Warriors",
            game_start_time="not-a-valid-date",
            end_date_iso=_hours_from_now(2.0),          # ≤ 4h block
        )
        sm = classify_market(m, score_market(m, hours=24))
        self.assertEqual(sm.action, "avoid")
        # Reason must come from Phase 3 (end_date), not Phase 1.
        self.assertIn("expiring", sm.reason.lower())
        self.assertNotIn("kickoff", sm.reason.lower())

    def test_rf_game_block_hours_zero_disables_phase1(self):
        """Setting RF_GAME_BLOCK_HOURS=0 must disable Phase 1 entirely,
        even for a market that would otherwise be blocked (kickoff imminent)."""
        from oversight.market_scorer import classify_market, score_market
        m = _make_market_metrics(
            question="Lakers vs Celtics",
            daily_rate=50.0,
            game_start_time=_hours_from_now(0.25),     # 15min from kickoff
            end_date_iso=_hours_from_now(10.0),         # safe
            on_book_hours=24.0,
        )
        # Monkeypatch the constant; classify_market re-imports it each call.
        import config
        original = config.RF_GAME_BLOCK_HOURS
        config.RF_GAME_BLOCK_HOURS = 0.0
        try:
            sm = classify_market(m, score_market(m, hours=24))
            # With Phase 1 disabled, the market deploys (end_date_iso=10h > 4h,
            # and > 72h cap doesn't apply either since 10h < 72h → min_size cap).
            self.assertEqual(sm.action, "deploy")
            self.assertNotIn("kickoff", sm.reason.lower())
        finally:
            config.RF_GAME_BLOCK_HOURS = original


# ═══════════════════════════════════════════════════════════════════════
# Layer 2: Bot (order_lifecycle)
# ═══════════════════════════════════════════════════════════════════════

class TestLayer2BotSportsBlock(unittest.TestCase):
    """order_lifecycle.place_orders_for_market() blocks dangerous sports markets."""

    @patch("order_lifecycle.get_merged_book")
    def test_sports_no_end_date_blocked(self, mock_book):
        """Sports market with no end_date → blocked, feedback written."""
        mock_book.return_value = {
            "bids": [{"price": "0.48", "size": "500"}],
            "asks": [{"price": "0.52", "size": "500"}],
        }
        ms = _make_ms(question="Lakers vs Celtics — who wins?", end_date_iso="")
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)
        ol.place_orders_for_market(ms)

        # Should have written "skipped" feedback with "sports_no_expiry" reason
        calls = ol.db.write_placement_feedback.call_args_list
        reasons = [c[0][3] for c in calls]  # 4th arg is reason
        self.assertTrue(
            any("sports" in r for r in reasons),
            f"Expected sports block reason in feedback, got: {reasons}"
        )

    @patch("order_lifecycle.get_merged_book")
    def test_sports_expiring_2h_blocked(self, mock_book):
        """Sports market expiring in 2h → blocked."""
        mock_book.return_value = {
            "bids": [{"price": "0.48", "size": "500"}],
            "asks": [{"price": "0.52", "size": "500"}],
        }
        ms = _make_ms(
            question="Man City vs Arsenal — PL match",
            end_date_iso=_hours_from_now(2.0),
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)
        ol.place_orders_for_market(ms)

        calls = ol.db.write_placement_feedback.call_args_list
        reasons = [c[0][3] for c in calls]
        self.assertTrue(
            any("live_sports" in r for r in reasons),
            f"Expected live_sports reason, got: {reasons}"
        )

    @patch("order_lifecycle.get_merged_book")
    def test_sports_expiring_10h_allowed(self, mock_book):
        """Sports market expiring in 10h (> 4h) → allowed through."""
        mock_book.return_value = {
            "bids": [{"price": "0.48", "size": "500"}],
            "asks": [{"price": "0.52", "size": "500"}],
        }
        ms = _make_ms(
            question="Lakers vs Celtics — NBA game",
            end_date_iso=_hours_from_now(10.0),
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)
        ol.place_orders_for_market(ms)

        # Should NOT have sports block feedback — it either placed or skipped
        # for non-sports reasons
        calls = ol.db.write_placement_feedback.call_args_list
        reasons = [c[0][3] for c in calls]
        self.assertFalse(
            any("sports" in r or "live_sports" in r for r in reasons),
            f"Sports market >4h should not be blocked, got: {reasons}"
        )

    @patch("order_lifecycle.get_merged_book")
    def test_non_sports_no_end_date_allowed(self, mock_book):
        """Non-sports market with no end_date → allowed through."""
        mock_book.return_value = {
            "bids": [{"price": "0.48", "size": "500"}],
            "asks": [{"price": "0.52", "size": "500"}],
        }
        ms = _make_ms(
            question="Will Bitcoin reach $200k?",
            end_date_iso="",
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)
        ol.place_orders_for_market(ms)

        calls = ol.db.write_placement_feedback.call_args_list
        reasons = [c[0][3] for c in calls]
        self.assertFalse(
            any("sports" in r for r in reasons),
            f"Non-sports market should not be sports-blocked, got: {reasons}"
        )

    @patch("order_lifecycle.get_merged_book")
    def test_sports_block_cancels_existing_orders(self, mock_book):
        """When sports block triggers, existing orders should be cancelled."""
        mock_book.return_value = {
            "bids": [{"price": "0.48", "size": "500"}],
            "asks": [{"price": "0.52", "size": "500"}],
        }
        ms = _make_ms(
            question="Team A vs Team B — who wins?",
            end_date_iso="",
        )
        ms.orders["yes"] = OrderSlot(order_id="existing_yes", price=0.48, shares=50, placed_at=time.time())
        ms.orders["no"] = OrderSlot(order_id="existing_no", price=0.52, shares=50, placed_at=time.time())

        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)
        ol.place_orders_for_market(ms)

        # Existing orders should be cleared
        self.assertIsNone(ms.orders["yes"].order_id)
        self.assertIsNone(ms.orders["no"].order_id)

    @patch("order_lifecycle.get_merged_book")
    def test_sports_bad_date_blocked(self, mock_book):
        """Sports market with unparseable date → blocked."""
        mock_book.return_value = {
            "bids": [{"price": "0.48", "size": "500"}],
            "asks": [{"price": "0.52", "size": "500"}],
        }
        ms = _make_ms(
            question="Will UFC 400 main event — who wins?",
            end_date_iso="not-a-real-date",
        )
        markets = {ms.cid: ms}
        ol = _make_lifecycle(markets)
        ol.place_orders_for_market(ms)

        calls = ol.db.write_placement_feedback.call_args_list
        reasons = [c[0][3] for c in calls]
        self.assertTrue(
            any("sports_bad_date" in r for r in reasons),
            f"Expected sports_bad_date reason, got: {reasons}"
        )


# ═══════════════════════════════════════════════════════════════════════
# Layer 3: Pre-cycle expiry sweep (reward_farmer)
# ═══════════════════════════════════════════════════════════════════════

class TestLayer3ExpirySweep(unittest.TestCase):
    """reward_farmer._sweep_expiring_markets() cancels orders on expiring markets."""

    def _make_farmer_stub(self, markets):
        """Create a minimal RewardFarmer-like object for testing the sweep."""
        # We can't easily instantiate RewardFarmer (needs CLOB client),
        # so we test _sweep_expiring_markets by calling it on a mock
        # that has the right attributes.
        from reward_farmer import RewardFarmer

        class FarmerStub:
            pass

        stub = FarmerStub()
        stub.markets = markets
        stub.order_lifecycle = MagicMock()
        stub.order_lifecycle.cancel_order.return_value = True
        stub.db = MagicMock()
        stub.dump_mgr = MagicMock()
        return stub

    def test_sweep_cancels_orders_expiring_in_30min(self):
        """Market expiring in 30min → all orders cancelled, agent_approved=False."""
        ms = _make_ms(
            question="Will ETH hit $5000?",
            end_date_iso=_hours_from_now(0.5),
        )
        ms.orders["yes"] = OrderSlot(order_id="oid_yes", price=0.48, shares=50, placed_at=time.time())
        ms.orders["no"] = OrderSlot(order_id="oid_no", price=0.52, shares=50, placed_at=time.time())
        ms.dump_orders["yes"] = "dump_oid_yes"
        ms.dump_state["yes"] = {"started_at": time.time(), "shares": 50, "tid": "ytid"}

        markets = {ms.cid: ms}
        stub = self._make_farmer_stub(markets)

        # Call the actual method on the stub
        from reward_farmer import RewardFarmer
        RewardFarmer._sweep_expiring_markets(stub)

        # Orders should be cleared
        self.assertIsNone(ms.orders["yes"].order_id)
        self.assertIsNone(ms.orders["no"].order_id)
        self.assertIsNone(ms.dump_orders["yes"])
        self.assertIsNone(ms.dump_state["yes"])
        self.assertFalse(ms.agent_approved)

    def test_sweep_ignores_market_expiring_in_3h(self):
        """Market expiring in 3h (> 1h threshold) → no action."""
        ms = _make_ms(
            question="Will ETH hit $5000?",
            end_date_iso=_hours_from_now(3.0),
        )
        ms.orders["yes"] = OrderSlot(order_id="oid_yes", price=0.48, shares=50, placed_at=time.time())

        markets = {ms.cid: ms}
        stub = self._make_farmer_stub(markets)

        from reward_farmer import RewardFarmer
        RewardFarmer._sweep_expiring_markets(stub)

        # Orders should be untouched
        self.assertEqual(ms.orders["yes"].order_id, "oid_yes")
        self.assertTrue(ms.agent_approved)

    def test_sweep_ignores_market_without_end_date(self):
        """Market with no end_date → no action (can't compute expiry)."""
        ms = _make_ms(
            question="Will Bitcoin reach $200k?",
            end_date_iso="",
        )
        ms.orders["yes"] = OrderSlot(order_id="oid_yes", price=0.48, shares=50, placed_at=time.time())

        markets = {ms.cid: ms}
        stub = self._make_farmer_stub(markets)

        from reward_farmer import RewardFarmer
        RewardFarmer._sweep_expiring_markets(stub)

        # Orders should be untouched
        self.assertEqual(ms.orders["yes"].order_id, "oid_yes")
        self.assertTrue(ms.agent_approved)

    def test_sweep_handles_already_expired_market(self):
        """Market already expired (negative hours) → swept."""
        ms = _make_ms(
            question="Will SOL flip ETH?",
            end_date_iso=_hours_from_now(-0.5),  # 30 min ago
        )
        ms.orders["yes"] = OrderSlot(order_id="oid_yes", price=0.48, shares=50, placed_at=time.time())

        markets = {ms.cid: ms}
        stub = self._make_farmer_stub(markets)

        from reward_farmer import RewardFarmer
        RewardFarmer._sweep_expiring_markets(stub)

        self.assertIsNone(ms.orders["yes"].order_id)
        self.assertFalse(ms.agent_approved)


# ═══════════════════════════════════════════════════════════════════════
# Sports keyword config
# ═══════════════════════════════════════════════════════════════════════

class TestSportsKeywords(unittest.TestCase):
    """Verify the shared SPORTS_KEYWORDS tuple is well-formed."""

    def test_keywords_is_tuple(self):
        """SPORTS_KEYWORDS should be a tuple (immutable)."""
        self.assertIsInstance(SPORTS_KEYWORDS, tuple)

    def test_keywords_not_empty(self):
        """Should have a meaningful number of keywords."""
        self.assertGreater(len(SPORTS_KEYWORDS), 20)

    def test_core_patterns_present(self):
        """Core patterns (with word-boundary padding) must be present."""
        for pattern in (" vs ", " nba", " nfl", "premier league", " ufc", " ipl"):
            self.assertIn(pattern, SPORTS_KEYWORDS, f"Missing core pattern: {pattern}")

    def test_keywords_are_lowercase(self):
        """All keywords should be lowercase for case-insensitive matching."""
        for kw in SPORTS_KEYWORDS:
            self.assertEqual(kw, kw.lower(), f"Keyword not lowercase: {kw!r}")


if __name__ == "__main__":
    unittest.main()
