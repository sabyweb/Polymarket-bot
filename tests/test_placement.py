"""Placement-formula regression tests (FX-036 — queue-depth-aware placement).

Pre-FX-036 the bot placed at the far edge of the reward zone (max_spread −
1 tick from midpoint), earning the lowest possible reward density (≈ 9%
of theoretical max on a 5.5¢ Iran-class market). FX-036 walks the merged
order book from best (closest to mid) outward, accumulates cumulative $
queue, and sits one tick behind the level where queue first reaches
``RF_TARGET_QUEUE_AHEAD_USD`` — much closer to mid, much higher reward
density, still shielded from fills by the queue we sit behind.

These tests pin the helper's contract:

- escape hatch (knob ≤ 0)              → legacy zone-edge behaviour
- thin book (queue < threshold in zone) → legacy zone-edge fallback
- empty book                            → legacy zone-edge fallback
- threshold met at first level          → sit one tick behind that level
- threshold met after N levels          → sit one tick behind level N
- zone-boundary edge case               → legacy fallback (never place outside zone)
- realistic Iran-market shape           → sits ≈ 2-3¢ from mid, not 4.5¢
- mirror symmetry (bid vs ask)          → behaviour invariant under flip
- end-to-end via place_orders_for_market → wiring exercises the helper

Plus invariants that should hold under every code path:

- the returned edges are always inside the reward zone
- the returned edges are clamped to ``[0.01, 0.99]``
- prices are rounded to ``decimals`` (no float drift in the API call)
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import MarketState
from order_lifecycle import (
    OrderLifecycle,
    _compute_edge_prices,
    _queue_aware_edge,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _book(bids, asks):
    """Build a merged-book dict with the post-FX-035 shape."""
    return {
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


def _iran_book():
    """Approximate the Iran market shape that surfaced FX-036.

    Midpoint ≈ 0.485, max_spread = 5.5¢. Bids stack heavily 1-3¢ below
    mid; asks stack heavily 1-3¢ above mid. By the time queue is exhausted
    inside the 5.5¢ zone we're well past $1k cumulative.
    """
    bids = [
        (0.48, 300),  # $144 — 1¢ from mid
        (0.47, 2200), # $1034 cumulative → threshold crossed here
        (0.46, 8000),
        (0.45, 10000),
        (0.44, 12000),
    ]
    asks = [
        (0.49, 400),
        (0.50, 2100), # cumulative ≈ $196 + $1050 = $1246 → threshold met
        (0.51, 8000),
        (0.52, 10000),
        (0.53, 12000),
    ]
    return _book(bids, asks)


def _make_ms(**overrides) -> MarketState:
    defaults = dict(
        cid="cid_iran", question="Iran closes its airspace by May 27?",
        yes_tid="ytid", no_tid="ntid",
        daily_rate=200.0, max_spread=0.055, min_size=10, tick_size=0.01,
        yes_price=0.485, agent_shares=50, agent_approved=True,
    )
    defaults.update(overrides)
    return MarketState(**defaults)


# ── _queue_aware_edge — direct unit tests ────────────────────────────────────


class TestQueueAwareEdgeEscapeHatches(unittest.TestCase):

    def test_returns_none_when_target_zero(self):
        """RF_TARGET_QUEUE_AHEAD_USD <= 0 disables queue-aware placement."""
        book = _iran_book()
        out = _queue_aware_edge(
            "bid", book["bids"], 0.485, 0.055, 0.01,
            target_queue_usd=0.0, decimals=2,
        )
        self.assertIsNone(out)

    def test_returns_none_when_target_negative(self):
        book = _iran_book()
        out = _queue_aware_edge(
            "bid", book["bids"], 0.485, 0.055, 0.01,
            target_queue_usd=-1.0, decimals=2,
        )
        self.assertIsNone(out)

    def test_returns_none_on_empty_book(self):
        out = _queue_aware_edge(
            "bid", [], 0.50, 0.05, 0.01,
            target_queue_usd=1000.0, decimals=2,
        )
        self.assertIsNone(out)


class TestQueueAwareEdgeBidSide(unittest.TestCase):

    def test_threshold_met_at_first_level(self):
        """First level alone exceeds threshold → sit 1 tick behind it."""
        # 0.48 × 5000 = $2400 > $1000 threshold
        bids = [(0.48, 5000), (0.47, 100)]
        out = _queue_aware_edge(
            "bid", [{"price": p, "size": s} for p, s in bids],
            0.485, 0.055, 0.01, target_queue_usd=1000.0, decimals=2,
        )
        # 0.48 - 0.01 = 0.47
        self.assertAlmostEqual(out, 0.47, places=4)

    def test_threshold_met_at_second_level(self):
        """First level under threshold, cumulative crosses at second."""
        # 0.48 × 100 = $48; + 0.47 × 2200 = $1034 + $48 = $1082 > $1000
        bids = [(0.48, 100), (0.47, 2200), (0.46, 8000)]
        out = _queue_aware_edge(
            "bid", [{"price": p, "size": s} for p, s in bids],
            0.485, 0.055, 0.01, target_queue_usd=1000.0, decimals=2,
        )
        # 0.47 - 0.01 = 0.46
        self.assertAlmostEqual(out, 0.46, places=4)

    def test_thin_book_falls_back(self):
        """Cumulative never reaches threshold → return None (legacy fallback)."""
        bids = [(0.48, 100), (0.47, 100), (0.46, 100), (0.45, 100), (0.44, 100)]
        # Cumulative ≈ $235 across the zone, well below $1000
        out = _queue_aware_edge(
            "bid", [{"price": p, "size": s} for p, s in bids],
            0.485, 0.055, 0.01, target_queue_usd=1000.0, decimals=2,
        )
        self.assertIsNone(out)

    def test_zone_boundary_breaks_walk(self):
        """Once we step out of the reward zone we stop walking even if more
        levels exist deeper in the book."""
        # Midpoint=0.50, max_spread=0.03 → zone = [0.47, 0.53]
        # 0.48 in zone (d=0.02 < 0.03); 0.46 out of zone (d=0.04 >= 0.03)
        bids = [(0.48, 100), (0.46, 100000)]  # would cross threshold but is out-of-zone
        out = _queue_aware_edge(
            "bid", [{"price": p, "size": s} for p, s in bids],
            0.50, 0.03, 0.01, target_queue_usd=1000.0, decimals=2,
        )
        self.assertIsNone(out)

    def test_tick_step_would_exit_zone_falls_back(self):
        """Threshold met at the very last in-zone level — `price - tick`
        would itself be outside the zone. Helper falls back rather than
        placing illegally."""
        # Zone [0.47, 0.53], tick=0.01. Place a huge level right at 0.47 boundary.
        # 0.47 - 0.01 = 0.46, which is at d=0.04 from mid 0.50 — outside max_spread=0.03.
        bids = [(0.47, 100000)]  # threshold met but step exits zone
        out = _queue_aware_edge(
            "bid", [{"price": p, "size": s} for p, s in bids],
            0.50, 0.03, 0.01, target_queue_usd=1000.0, decimals=2,
        )
        # 0.47 is technically d=0.03 from mid which is >= max_spread, so the
        # zone-check at the top of the loop fires first.
        self.assertIsNone(out)


class TestQueueAwareEdgeAskSide(unittest.TestCase):
    """Mirror symmetry — ask side should behave identically with flipped sign."""

    def test_threshold_met_at_first_level(self):
        asks = [(0.52, 5000), (0.53, 100)]
        out = _queue_aware_edge(
            "ask", [{"price": p, "size": s} for p, s in asks],
            0.515, 0.055, 0.01, target_queue_usd=1000.0, decimals=2,
        )
        # 0.52 + 0.01 = 0.53
        self.assertAlmostEqual(out, 0.53, places=4)

    def test_threshold_met_at_second_level(self):
        asks = [(0.52, 100), (0.53, 2200), (0.54, 8000)]
        out = _queue_aware_edge(
            "ask", [{"price": p, "size": s} for p, s in asks],
            0.515, 0.055, 0.01, target_queue_usd=1000.0, decimals=2,
        )
        # 0.53 + 0.01 = 0.54
        self.assertAlmostEqual(out, 0.54, places=4)

    def test_thin_book_falls_back(self):
        asks = [(0.52, 50)] * 5
        out = _queue_aware_edge(
            "ask", [{"price": p, "size": s} for p, s in asks],
            0.515, 0.055, 0.01, target_queue_usd=1000.0, decimals=2,
        )
        self.assertIsNone(out)


class TestQueueAwareEdgeInputCoercion(unittest.TestCase):
    """The merged book mostly arrives as floats post-FX-035 but test fixtures
    and historical paths sometimes pass strings. Helper coerces."""

    def test_string_prices_and_sizes(self):
        bids = [{"price": "0.48", "size": "5000"}]
        out = _queue_aware_edge(
            "bid", bids, 0.485, 0.055, 0.01,
            target_queue_usd=1000.0, decimals=2,
        )
        self.assertAlmostEqual(out, 0.47, places=4)

    def test_malformed_level_is_skipped(self):
        bids = [
            {"size": "5000"},               # missing price
            {"price": "0.47", "size": "2200"},
        ]
        out = _queue_aware_edge(
            "bid", bids, 0.485, 0.055, 0.01,
            target_queue_usd=1000.0, decimals=2,
        )
        # First level skipped via KeyError → second level becomes effectively
        # the first; 0.47 × 2200 = $1034 > $1000 → return 0.47 - 0.01 = 0.46
        self.assertAlmostEqual(out, 0.46, places=4)


class TestQueueAwareEdgeTickVariations(unittest.TestCase):

    def test_sub_cent_tick(self):
        """Some markets use 0.001 tick. Edge rounds to the right decimals."""
        bids = [{"price": 0.488, "size": 5000}]
        out = _queue_aware_edge(
            "bid", bids, 0.490, 0.020, 0.001,
            target_queue_usd=1000.0, decimals=3,
        )
        # 0.488 - 0.001 = 0.487; inside zone (d=0.003 < 0.020)
        self.assertAlmostEqual(out, 0.487, places=4)


# ── _compute_edge_prices — composite tests ───────────────────────────────────


class TestComputeEdgePricesEscapeHatch(unittest.TestCase):

    def test_target_zero_uses_legacy_formula(self):
        """RF_TARGET_QUEUE_AHEAD_USD = 0 → byte-identical to pre-FX-036."""
        book = _iran_book()
        edge_bid, edge_ask = _compute_edge_prices(
            merged=book, midpoint=0.485, max_spread=0.055, tick=0.01,
            decimals=2, ticks_inside=1, target_queue_usd=0.0,
        )
        # Pre-FX-036: midpoint - max_spread + tick = 0.485 - 0.055 + 0.01 = 0.44
        # Pre-FX-036: midpoint + max_spread - tick = 0.485 + 0.055 - 0.01 = 0.53
        self.assertAlmostEqual(edge_bid, 0.44, places=4)
        self.assertAlmostEqual(edge_ask, 0.53, places=4)


class TestComputeEdgePricesIranMarket(unittest.TestCase):
    """The motivating scenario — Iran market with deep books."""

    def test_iran_market_sits_close_to_mid(self):
        book = _iran_book()
        edge_bid, edge_ask = _compute_edge_prices(
            merged=book, midpoint=0.485, max_spread=0.055, tick=0.01,
            decimals=2, ticks_inside=1, target_queue_usd=1000.0,
        )
        # Pre-FX-036 placed at 0.44 / 0.53 (4.5¢ from mid, ~9% reward density).
        # Post-FX-036 should sit ~2¢ from mid given the queue distribution.
        # Bid: 0.48 × 300 + 0.47 × 2200 = $144 + $1034 = $1178 > $1000
        # → return 0.47 - 0.01 = 0.46
        # Ask: 0.49 × 400 + 0.50 × 2100 = $196 + $1050 = $1246 > $1000
        # → return 0.50 + 0.01 = 0.51
        self.assertAlmostEqual(edge_bid, 0.46, places=4)
        self.assertAlmostEqual(edge_ask, 0.51, places=4)

    def test_reward_density_uplift_vs_legacy(self):
        """Sanity check: the new placement earns multiples more reward density."""
        book = _iran_book()
        # Reward density ∝ (1 - d/max_spread); higher is better
        eb_new, ea_new = _compute_edge_prices(
            merged=book, midpoint=0.485, max_spread=0.055, tick=0.01,
            decimals=2, ticks_inside=1, target_queue_usd=1000.0,
        )
        eb_old, ea_old = _compute_edge_prices(
            merged=book, midpoint=0.485, max_spread=0.055, tick=0.01,
            decimals=2, ticks_inside=1, target_queue_usd=0.0,
        )
        d_new = abs(0.485 - eb_new)
        d_old = abs(0.485 - eb_old)
        density_new = 1 - d_new / 0.055
        density_old = 1 - d_old / 0.055
        # density_new ≈ 1 - 0.025/0.055 ≈ 0.545; density_old ≈ 1 - 0.045/0.055 ≈ 0.182
        # ratio ≈ 3.0× — well above the 2× sanity floor
        self.assertGreater(density_new / max(density_old, 1e-6), 2.0)


class TestComputeEdgePricesThinBook(unittest.TestCase):
    """Weather-style markets — low competition, thin queue → legacy fallback,
    no behaviour change for that regime (memory: weather markets fill quickly
    regardless of queue, so the existing min_size + dump-on-fill flow keeps
    working unchanged)."""

    def test_thin_book_falls_back_to_legacy(self):
        thin = _book(
            bids=[(0.48, 50), (0.47, 50)],
            asks=[(0.52, 50), (0.53, 50)],
        )
        edge_bid, edge_ask = _compute_edge_prices(
            merged=thin, midpoint=0.50, max_spread=0.055, tick=0.01,
            decimals=2, ticks_inside=1, target_queue_usd=1000.0,
        )
        # Legacy zone-edge placement, rounded to decimals=2:
        # round(0.50 - 0.055 + 0.01, 2) → round(0.455, 2) → 0.46 (banker's)
        # round(0.50 + 0.055 - 0.01, 2) → round(0.545, 2) → 0.55
        self.assertAlmostEqual(edge_bid, 0.46, places=4)
        self.assertAlmostEqual(edge_ask, 0.55, places=4)


class TestComputeEdgePricesAsymmetry(unittest.TestCase):
    """Different queue depth on bid vs ask → asymmetric edges are fine."""

    def test_bid_queue_aware_ask_legacy(self):
        asym = _book(
            bids=[(0.48, 5000)],  # threshold met
            asks=[(0.52, 50), (0.53, 50)],  # thin
        )
        edge_bid, edge_ask = _compute_edge_prices(
            merged=asym, midpoint=0.50, max_spread=0.055, tick=0.01,
            decimals=2, ticks_inside=1, target_queue_usd=1000.0,
        )
        self.assertAlmostEqual(edge_bid, 0.47, places=4)   # queue-aware: 0.48 − tick
        self.assertAlmostEqual(edge_ask, 0.55, places=4)   # legacy fallback (banker's round)


class TestComputeEdgePricesSafetyInvariants(unittest.TestCase):
    """Guarantees that must hold for every input: inside zone, clamped."""

    def test_edges_always_inside_zone(self):
        """Random-ish books across a few scenarios — edges stay inside zone."""
        scenarios = [
            (_iran_book(), 0.485, 0.055, 0.01),
            (_book(bids=[(0.48, 5000)], asks=[(0.52, 5000)]), 0.50, 0.03, 0.01),
            (_book(bids=[(0.10, 10000)], asks=[(0.90, 10000)]), 0.50, 0.40, 0.01),
        ]
        for merged, mid, ms, tick in scenarios:
            with self.subTest(midpoint=mid, max_spread=ms):
                eb, ea = _compute_edge_prices(
                    merged=merged, midpoint=mid, max_spread=ms, tick=tick,
                    decimals=2, ticks_inside=1, target_queue_usd=1000.0,
                )
                self.assertLess(abs(mid - eb), ms,
                                f"bid {eb} not inside zone for mid={mid} ms={ms}")
                self.assertLess(abs(ea - mid), ms,
                                f"ask {ea} not inside zone for mid={mid} ms={ms}")

    def test_edges_clamped_to_legal_range(self):
        """Even with degenerate inputs the final clamp keeps prices legal."""
        # Tiny-mid market with huge zone — legacy formula would produce negative
        # bid; the [0.01, 0.99] clamp catches it.
        merged = _book(bids=[(0.05, 50)], asks=[(0.15, 50)])
        eb, ea = _compute_edge_prices(
            merged=merged, midpoint=0.10, max_spread=0.20, tick=0.01,
            decimals=2, ticks_inside=1, target_queue_usd=1000.0,
        )
        self.assertGreaterEqual(eb, 0.01)
        self.assertLessEqual(ea, 0.99)


# ── place_orders_for_market — end-to-end wiring ──────────────────────────────


def _drop_stale_clob_mocks():
    """Drop partial sys.modules mocks of py_clob_client_v2 left by sibling tests.

    test_critical_fixes.py and test_sports_protection.py patch
    ``sys.modules["py_clob_client_v2"]`` with a MagicMock to make
    `from py_clob_client_v2.clob_types import OrderArgs` work in environments
    without the real SDK. They never clean up, and the partial mocks are
    missing ``order_builder.constants``, so once they've run our tests can't
    re-import the real SDK. Dropping the stale entries here lets Python's
    import machinery rediscover the real package on the dev / CI venv.
    """
    import sys
    from unittest.mock import MagicMock
    keys_to_drop = [
        k for k in list(sys.modules)
        if k == "py_clob_client_v2" or k.startswith("py_clob_client_v2.")
        if isinstance(sys.modules[k], MagicMock)
    ]
    for k in keys_to_drop:
        del sys.modules[k]


def _make_lifecycle(ms: MarketState):
    """OrderLifecycle stub mirroring tests/test_order_lifecycle.py."""
    positions = MagicMock()
    positions.get_shares.return_value = 0
    positions.can_quote.return_value = True
    db = MagicMock()
    db.is_unliquidatable.return_value = False
    ol = OrderLifecycle(
        client=MagicMock(), db=db, positions=positions,
        rewards=MagicMock(), markets={ms.cid: ms}, dry_run=False,
    )
    ol.capital_ceiling = None
    return ol


class TestPlaceOrdersForMarketUsesQueueAware(unittest.TestCase):
    """End-to-end: verify the wiring exercises _compute_edge_prices."""

    def setUp(self):
        _drop_stale_clob_mocks()

    @patch("order_lifecycle.get_merged_book")
    def test_iran_market_places_close_to_mid(self, mock_book):
        mock_book.return_value = _iran_book()
        ms = _make_ms()
        ol = _make_lifecycle(ms)
        placed_oids = iter([{"orderID": "OID_YES"}, {"orderID": "OID_NO"}])
        ol.client.create_and_post_order.side_effect = lambda *_a, **_kw: next(placed_oids)

        self.assertEqual(2, ol.place_orders_for_market(ms))

        # First call should be YES bid at 0.46 (queue-aware), not 0.44 (legacy)
        first_call_args = ol.client.create_and_post_order.call_args_list[0]
        order_args = first_call_args[0][0]
        self.assertAlmostEqual(order_args.price, 0.46, places=4)

    @patch("order_lifecycle.get_merged_book")
    def test_thin_market_falls_back_to_legacy_placement(self, mock_book):
        # MarketState midpoint = (best_bid + best_ask) / 2 = 0.485 for the
        # Iran-like fixture below. Use a deliberately *non*-Iran midpoint
        # here so the legacy-fallback price is distinguishable from the
        # queue-aware Iran case in the sibling test (which lands at 0.46).
        thin = _book(
            bids=[(0.50, 50), (0.49, 50)],
            asks=[(0.52, 50), (0.53, 50)],
        )
        mock_book.return_value = thin
        ms = _make_ms(max_spread=0.05, tick_size=0.01)  # midpoint will be 0.51
        ol = _make_lifecycle(ms)
        placed_oids = iter([{"orderID": "OID_YES"}, {"orderID": "OID_NO"}])
        ol.client.create_and_post_order.side_effect = lambda *_a, **_kw: next(placed_oids)

        ol.place_orders_for_market(ms)

        first_call_args = ol.client.create_and_post_order.call_args_list[0]
        order_args = first_call_args[0][0]
        # midpoint = (0.50 + 0.52)/2 = 0.51; legacy = 0.51 - 0.05 + 0.01 = 0.47
        self.assertAlmostEqual(order_args.price, 0.47, places=4)

    @patch("order_lifecycle.get_merged_book")
    def test_escape_hatch_reproduces_legacy_placement(self, mock_book):
        """When the operator sets RF_TARGET_QUEUE_AHEAD_USD=0 the bot must
        place at the same prices it did pre-FX-036."""
        mock_book.return_value = _iran_book()
        ms = _make_ms()
        ol = _make_lifecycle(ms)
        placed_oids = iter([{"orderID": "OID_YES"}, {"orderID": "OID_NO"}])
        ol.client.create_and_post_order.side_effect = lambda *_a, **_kw: next(placed_oids)
        with patch("order_lifecycle.TARGET_QUEUE_AHEAD_USD", lambda: 0.0):
            ol.place_orders_for_market(ms)

        first_call_args = ol.client.create_and_post_order.call_args_list[0]
        order_args = first_call_args[0][0]
        # Legacy: 0.485 - 0.055 + 0.01 = 0.44
        self.assertAlmostEqual(order_args.price, 0.44, places=4)


if __name__ == "__main__":
    unittest.main()
