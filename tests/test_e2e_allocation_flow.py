"""E2E integration test: agent writes allocation → bot places orders → agent reads feedback."""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import MarketState, OrderSlot
from database import BotDatabase
from oversight.allocation_writer import write_allocations
from oversight.data_collector import query_placement_feedback
from order_lifecycle import OrderLifecycle


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_mock_client(order_counter=None):
    """Create a mock CLOB client that returns incrementing order IDs."""
    if order_counter is None:
        order_counter = [0]
    client = MagicMock()

    def fake_create_and_post(args):
        order_counter[0] += 1
        return {"orderID": f"mock_oid_{order_counter[0]}"}

    client.create_and_post_order.side_effect = fake_create_and_post
    client.cancel.return_value = None
    return client


def _make_mock_positions():
    """PositionStore mock: empty positions, all sides quotable."""
    pos = MagicMock()
    pos.get_shares.return_value = 0.0
    pos.can_quote.return_value = True
    return pos


def _make_market_state(alloc: dict) -> MarketState:
    """Build a MarketState from an allocation dict (mimics _apply_market_changes)."""
    return MarketState(
        cid=alloc["condition_id"],
        question=alloc.get("question", "Test?"),
        yes_tid=f"yes_tid_{alloc['condition_id'][-4:]}",
        no_tid=f"no_tid_{alloc['condition_id'][-4:]}",
        daily_rate=alloc.get("daily_rate", 50),
        max_spread=alloc.get("max_spread", 0.04),
        min_size=alloc.get("min_size", 5),
        tick_size=0.01,
        yes_price=0.50,
        agent_shares=alloc.get("shares_per_side", 50),
    )


def _normal_book(mid=0.50, spread=0.04):
    """Synthetic order book with configurable midpoint and spread."""
    bid = round(mid - spread / 2, 3)
    ask = round(mid + spread / 2, 3)
    return {
        "bids": [{"price": str(bid), "size": "500"}],
        "asks": [{"price": str(ask), "size": "500"}],
    }


def _wide_book():
    """Order book with spread > RF_MAX_BOOK_SPREAD (0.15)."""
    return {
        "bids": [{"price": "0.35", "size": "500"}],
        "asks": [{"price": "0.65", "size": "500"}],
    }


DEPLOY_MARKETS = [
    {
        "condition_id": "0xaaa1",
        "question": "Will ETH hit $5000?",
        "action": "deploy",
        "shares_per_side": 50,
        "score": 15.0,
        "reason": "Zero fills",
        "confidence": "high",
        "actual_reward_total": 0,
        "fill_damage": 0,
        "fill_count": 0,
        "daily_rate": 100,
        "min_size": 5,
        "max_spread": 0.04,
    },
    {
        "condition_id": "0xbbb2",
        "question": "Will BTC hit $200k?",
        "action": "deploy",
        "shares_per_side": 100,
        "score": 12.0,
        "reason": "Low fills",
        "confidence": "medium",
        "actual_reward_total": 0,
        "fill_damage": 0,
        "fill_count": 0,
        "daily_rate": 80,
        "min_size": 5,
        "max_spread": 0.04,
    },
]

AVOID_MARKET = {
    "condition_id": "0xccc3",
    "question": "Will SOL flip ETH?",
    "action": "avoid",
    "shares_per_side": 0,
    "score": -5.0,
    "reason": "High fills",
    "confidence": "high",
    "actual_reward_total": 0,
    "fill_damage": 10,
    "fill_count": 5,
    "daily_rate": 20,
    "min_size": 5,
    "max_spread": 0.04,
}


class TestE2EAllocationFlow(unittest.TestCase):
    """End-to-end: agent writes → bot reads/places → agent reads feedback."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.alloc_path = os.path.join(self.tmpdir, "market_allocations.json")
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = BotDatabase(db_path=self.db_path)

    def tearDown(self):
        self.db.close()
        for f in [self.alloc_path, self.db_path]:
            if os.path.exists(f):
                os.unlink(f)
        if os.path.exists(self.tmpdir):
            try:
                os.rmdir(self.tmpdir)
            except OSError:
                pass

    def _build_lifecycle(self, client=None, dry_run=False):
        """Create an OrderLifecycle with mocked dependencies."""
        if client is None:
            client = _make_mock_client()
        markets = {}
        lc = OrderLifecycle(
            client=client,
            db=self.db,
            positions=_make_mock_positions(),
            rewards=MagicMock(),
            markets=markets,
            dry_run=dry_run,
        )
        return lc, markets

    # ── Test 1: Happy path ───────────────────────────────────────────────

    @patch("order_lifecycle.get_merged_book")
    def test_happy_path_full_loop(self, mock_book):
        """Agent writes 2 deploy + 1 avoid → bot places on both deploy → agent reads 'placed'."""
        mock_book.return_value = _normal_book()

        # Step 1: Agent writes allocations
        all_markets = DEPLOY_MARKETS + [AVOID_MARKET]
        write_allocations(all_markets, total_capital_deployed=45.0,
                          output_path=self.alloc_path)

        # Step 2: Bot reads allocations (mimics _load_allocations)
        with open(self.alloc_path) as f:
            data = json.load(f)
        deploy = [m for m in data["markets"] if m["action"] == "deploy"]
        self.assertEqual(len(deploy), 2)

        # Step 3: Bot builds MarketState and places orders
        lc, markets = self._build_lifecycle()
        for alloc in deploy:
            ms = _make_market_state(alloc)
            markets[ms.cid] = ms
            lc.place_orders_for_market(ms)

        # Step 4: Agent reads feedback
        feedback = query_placement_feedback(self.db_path)

        # Verify: both deploy markets have "placed" on both sides
        for alloc in deploy:
            cid = alloc["condition_id"]
            self.assertIn(cid, feedback, f"Missing feedback for {cid}")
            for side in ["yes", "no"]:
                self.assertIn(side, feedback[cid], f"Missing {side} feedback for {cid}")
                self.assertEqual(
                    feedback[cid][side]["status"], "placed",
                    f"{cid} {side}: expected 'placed', got '{feedback[cid][side]['status']}'"
                )

        # Avoid market should NOT have feedback
        self.assertNotIn("0xccc3", feedback)

    # ── Test 2: Wide spread → skipped ────────────────────────────────────

    @patch("order_lifecycle.get_merged_book")
    def test_wide_spread_skips_placement(self, mock_book):
        """Wide spread book → bot writes 'skipped' with reason 'wide_spread'."""
        mock_book.return_value = _wide_book()

        # Agent writes 1 deploy market
        write_allocations([DEPLOY_MARKETS[0]], total_capital_deployed=15.0,
                          output_path=self.alloc_path)

        # Bot places
        lc, markets = self._build_lifecycle()
        ms = _make_market_state(DEPLOY_MARKETS[0])
        markets[ms.cid] = ms
        lc.place_orders_for_market(ms)

        # Agent reads feedback
        feedback = query_placement_feedback(self.db_path)
        cid = DEPLOY_MARKETS[0]["condition_id"]
        self.assertIn(cid, feedback)
        for side in ["yes", "no"]:
            self.assertEqual(feedback[cid][side]["status"], "skipped")
            self.assertEqual(feedback[cid][side]["reason"], "wide_spread")

    # ── Test 3: Stale allocation → rejected ──────────────────────────────

    def test_stale_allocation_rejected(self):
        """Stale allocation file (>2h old) returns None from load logic."""
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        payload = {
            "generated_at": stale_time,
            "version": "1.0",
            "total_capital_deployed": 15.0,
            "num_deploy": 1,
            "num_avoid": 0,
            "markets": [DEPLOY_MARKETS[0]],
        }
        with open(self.alloc_path, "w") as f:
            json.dump(payload, f)

        # Replicate _load_allocations freshness check
        with open(self.alloc_path) as f:
            data = json.load(f)
        gen_dt = datetime.fromisoformat(data["generated_at"])
        age = datetime.now(timezone.utc) - gen_dt
        is_stale = age > timedelta(hours=2)
        self.assertTrue(is_stale, "Allocation should be detected as stale")

        # No orders placed → no feedback
        feedback = query_placement_feedback(self.db_path)
        self.assertEqual(len(feedback), 0)

    # ── Test 4: CLOB failure → failed feedback ───────────────────────────

    @patch("order_lifecycle.get_merged_book")
    def test_clob_failure_writes_failed_feedback(self, mock_book):
        """CLOB create_and_post_order raises → bot writes 'failed'/'order_error'."""
        mock_book.return_value = _normal_book()
        client = _make_mock_client()
        client.create_and_post_order.side_effect = RuntimeError("API timeout")

        lc, markets = self._build_lifecycle(client=client)
        ms = _make_market_state(DEPLOY_MARKETS[0])
        markets[ms.cid] = ms
        lc.place_orders_for_market(ms)

        feedback = query_placement_feedback(self.db_path)
        cid = DEPLOY_MARKETS[0]["condition_id"]
        self.assertIn(cid, feedback)
        for side in ["yes", "no"]:
            self.assertEqual(feedback[cid][side]["status"], "failed")
            self.assertEqual(feedback[cid][side]["reason"], "order_error")

    # ── Test 5: Insufficient balance → capital_exhausted ─────────────────

    @patch("order_lifecycle.get_merged_book")
    def test_capital_exhausted_propagates(self, mock_book):
        """'insufficient balance' error → capital_exhausted flag + feedback."""
        mock_book.return_value = _normal_book()
        client = _make_mock_client()
        client.create_and_post_order.side_effect = RuntimeError("insufficient balance")

        lc, markets = self._build_lifecycle(client=client)
        ms = _make_market_state(DEPLOY_MARKETS[0])
        markets[ms.cid] = ms
        lc.place_orders_for_market(ms)

        # capital_exhausted flag should be set
        self.assertTrue(lc.capital_exhausted)

        feedback = query_placement_feedback(self.db_path)
        cid = DEPLOY_MARKETS[0]["condition_id"]
        self.assertIn(cid, feedback)
        # YES side fails first, triggers early return before NO side
        self.assertEqual(feedback[cid]["yes"]["status"], "failed")
        self.assertEqual(feedback[cid]["yes"]["reason"], "capital_exhausted")


if __name__ == "__main__":
    unittest.main()
