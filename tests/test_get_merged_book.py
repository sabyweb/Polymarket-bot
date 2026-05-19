"""Regression tests for `market_discovery.get_merged_book` — fixit.md::FX-035.

Background: py-clob-client-v2 v1.0.0 returns `client.get_order_book()` as a
**dict** with string-valued `'bids'`/`'asks'` entries like
``[{'price': '0.02', 'size': '2250'}, ...]``. Pre-FX-035 the production
code (`market_discovery.get_merged_book`) used `getattr(ob, "bids", [])`
and `float(b.price)` — assuming an object-with-attributes shape (the
format that test mocks happen to produce). On the real SDK return,
`getattr(dict, "bids")` returned `[]`, so `get_merged_book` always
returned `None`. The bot couldn't fetch a single book in production for
4 days; FX-016's 152 SafetyController tests + everything else stayed
green because every existing test mocks `get_merged_book` itself rather
than calling the real function with realistic SDK input.

These tests fix that gap. They call the real `get_merged_book` with a
stub `client` that returns the exact dict shape the V2 SDK produces in
production (verified via direct SDK call on Helsinki 2026-05-19 04:36
UTC). They also verify backward-compatibility with the object-form
mocks that the rest of the suite uses.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from market_discovery import _book_entries, get_merged_book


# ────────────────────────────────────────────────────────────────────────────
# Stub clients producing the two book shapes we care about
# ────────────────────────────────────────────────────────────────────────────


class _DictClient:
    """Returns books as dicts — the V2 SDK production shape."""

    def __init__(self, yes_book, no_book):
        self._yes = yes_book
        self._no = no_book

    def get_order_book(self, tid):
        # Simple lookup — the first tid is YES, the second is NO. Caller
        # arranges these.
        if tid == "yes_tid":
            return self._yes
        if tid == "no_tid":
            return self._no
        return None


class _ObjectClient:
    """Returns books as SimpleNamespace objects — the test-mock shape."""

    def __init__(self, yes_book, no_book):
        self._yes = yes_book
        self._no = no_book

    def get_order_book(self, tid):
        if tid == "yes_tid":
            return self._yes
        if tid == "no_tid":
            return self._no
        return None


# ────────────────────────────────────────────────────────────────────────────
# _book_entries normalizer
# ────────────────────────────────────────────────────────────────────────────


class TestBookEntriesNormalizer(unittest.TestCase):
    """`_book_entries` extracts (price, size) tuples from either form."""

    def test_dict_form_with_string_values_v2_sdk_shape(self):
        # Exact shape py-clob-client-v2 v1.0.0 returns in production
        ob = {
            "market": "0xabc",
            "asset_id": "12345",
            "timestamp": "1779165454000",
            "hash": "abcdef",
            "bids": [
                {"price": "0.29", "size": "100"},
                {"price": "0.28", "size": "200"},
            ],
            "asks": [
                {"price": "0.31", "size": "150"},
            ],
        }
        bids = _book_entries(ob, "bids")
        asks = _book_entries(ob, "asks")
        self.assertEqual([(0.29, 100.0), (0.28, 200.0)], bids)
        self.assertEqual([(0.31, 150.0)], asks)

    def test_object_form_with_float_attrs_test_mock_shape(self):
        # Shape that test mocks produce (legacy assumption)
        ob = SimpleNamespace(
            bids=[
                SimpleNamespace(price=0.29, size=100.0),
                SimpleNamespace(price=0.28, size=200.0),
            ],
            asks=[
                SimpleNamespace(price=0.31, size=150.0),
            ],
        )
        bids = _book_entries(ob, "bids")
        asks = _book_entries(ob, "asks")
        self.assertEqual([(0.29, 100.0), (0.28, 200.0)], bids)
        self.assertEqual([(0.31, 150.0)], asks)

    def test_none_returns_empty(self):
        self.assertEqual([], _book_entries(None, "bids"))
        self.assertEqual([], _book_entries(None, "asks"))

    def test_dict_missing_key_returns_empty(self):
        self.assertEqual([], _book_entries({}, "bids"))
        self.assertEqual([], _book_entries({"asks": []}, "bids"))

    def test_object_missing_attr_returns_empty(self):
        self.assertEqual([], _book_entries(SimpleNamespace(), "bids"))


# ────────────────────────────────────────────────────────────────────────────
# get_merged_book end-to-end with both client shapes
# ────────────────────────────────────────────────────────────────────────────


class TestGetMergedBookV2SDKDictShape(unittest.TestCase):
    """The V2 SDK production shape MUST be handled correctly.

    Pre-FX-035 every test in this class would have failed (function returned
    None on a healthy market). Post-fix all pass.
    """

    def test_iran_market_realistic_shape(self):
        # Realistic V2 SDK return for the Iran market (the actual bug case
        # that locked Helsinki out for 4 days). Books shaped like the live
        # API: YES outcome trades ~0.29, NO trades ~0.71.
        yes_book = {
            "market": "0xd9933a54c518...",
            "asset_id": "yes_asset",
            "timestamp": "1779165454000",
            "hash": "deadbeef",
            "bids": [
                {"price": "0.29", "size": "100"},
                {"price": "0.28", "size": "200"},
                {"price": "0.27", "size": "300"},
            ],
            "asks": [
                {"price": "0.31", "size": "150"},
                {"price": "0.32", "size": "250"},
            ],
        }
        no_book = {
            "market": "0xd9933a54c518...",
            "asset_id": "no_asset",
            "timestamp": "1779165454000",
            "hash": "cafebabe",
            "bids": [
                {"price": "0.71", "size": "100"},
                {"price": "0.70", "size": "200"},
            ],
            "asks": [
                {"price": "0.72", "size": "150"},
            ],
        }
        client = _DictClient(yes_book, no_book)
        result = get_merged_book(client, "yes_tid", "no_tid")
        self.assertIsNotNone(result, "FX-035: dict-form must not return None")
        # YES-side: 3 bids + 2 asks. Plus NO converted: 1 derived bid + 2 derived asks.
        # Total: 4 bids + 4 asks. (NO ask 0.72 → derived bid 0.28 — collides with YES bid at 0.28; both kept as separate entries.)
        self.assertEqual(4, len(result["bids"]))
        self.assertEqual(4, len(result["asks"]))
        # Top bid is the highest price — YES bid 0.29
        self.assertEqual(0.29, result["bids"][0]["price"])
        # Top ask is the lowest price — YES ask 0.31 (NO bid 0.71 → derived ask 0.29, but 0.29 < 0.31 so derived wins)
        self.assertEqual(0.29, result["asks"][0]["price"])

    def test_dict_form_yes_only_no_unavailable(self):
        # If NO book unavailable, return YES-only merged book.
        yes_book = {
            "bids": [{"price": "0.5", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
        }
        client = _DictClient(yes_book, None)
        result = get_merged_book(client, "yes_tid", "no_tid")
        self.assertIsNotNone(result)
        self.assertEqual(1, len(result["bids"]))
        self.assertEqual(1, len(result["asks"]))

    def test_dict_form_yes_unavailable_returns_none(self):
        # If YES book unavailable, can't merge → return None.
        client = _DictClient(None, {"bids": [{"price": "0.5", "size": "100"}]})
        result = get_merged_book(client, "yes_tid", "no_tid")
        self.assertIsNone(result)

    def test_dict_form_empty_books_returns_none(self):
        # Healthy SDK response but no liquidity yet → return None.
        client = _DictClient(
            {"bids": [], "asks": []},
            {"bids": [], "asks": []},
        )
        result = get_merged_book(client, "yes_tid", "no_tid")
        self.assertIsNone(result)

    def test_dict_form_string_prices_parsed_as_float(self):
        # Defensive: V2 SDK returns strings; our merged output should be floats.
        yes_book = {
            "bids": [{"price": "0.29", "size": "100"}],
            "asks": [{"price": "0.31", "size": "150"}],
        }
        client = _DictClient(yes_book, None)
        result = get_merged_book(client, "yes_tid", "no_tid")
        for entry in result["bids"] + result["asks"]:
            self.assertIsInstance(entry["price"], float)
            self.assertIsInstance(entry["size"], float)


class TestGetMergedBookObjectShape(unittest.TestCase):
    """Backward-compat: object-form mocks (used by ~200 existing tests) MUST
    still work.
    """

    def test_object_form_basic(self):
        yes_book = SimpleNamespace(
            bids=[SimpleNamespace(price=0.29, size=100.0)],
            asks=[SimpleNamespace(price=0.31, size=150.0)],
        )
        no_book = SimpleNamespace(
            bids=[SimpleNamespace(price=0.71, size=100.0)],
            asks=[SimpleNamespace(price=0.69, size=150.0)],
        )
        client = _ObjectClient(yes_book, no_book)
        result = get_merged_book(client, "yes_tid", "no_tid")
        self.assertIsNotNone(result, "object-form mocks must still work")
        # YES: 1 bid + 1 ask. NO ask 0.69 → derived bid 0.31. NO bid 0.71 → derived ask 0.29.
        self.assertEqual(2, len(result["bids"]))
        self.assertEqual(2, len(result["asks"]))


class TestGetMergedBookExceptionHandling(unittest.TestCase):
    """SDK exceptions should not crash — return None."""

    def test_exception_returns_none(self):
        client = MagicMock()
        client.get_order_book.side_effect = RuntimeError("network blip")
        result = get_merged_book(client, "yes_tid", "no_tid")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
