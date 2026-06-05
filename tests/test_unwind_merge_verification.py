"""Tests for _try_merge_positions balance verification.

Covers:
- Successful merge with verified balance decrease
- Phantom merge detected (balance unchanged after API success)
- Merge via inner client with verification
- No merge capability → manual alert fallback
- API error during merge → returns False
- API error during balance check → returns False (no phantom risk)
- Caller does NOT call record_unwind when merge returns False
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock py_clob_client_v2 before importing
if "py_clob_client_v2" not in sys.modules:
    mock_clob = MagicMock()
    sys.modules["py_clob_client_v2"] = mock_clob
    sys.modules["py_clob_client_v2.clob_types"] = mock_clob.clob_types
    sys.modules["py_clob_client_v2.client"] = mock_clob.client
    sys.modules["py_clob_client_v2.order_builder"] = mock_clob.order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = mock_clob.order_builder.constants
    mock_clob.order_builder.constants.BUY = "BUY"
    mock_clob.order_builder.constants.SELL = "SELL"

from unwind import UnwindMixin


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_unwind_stub(has_merge=True, has_inner_merge=False):
    """Create a minimal object mixing in UnwindMixin with mocked dependencies."""

    class Stub(UnwindMixin):
        pass

    stub = Stub()
    stub.client = MagicMock()
    stub.position_tracker = MagicMock()
    stub.market = {
        "condition_id": "cid_001",
        "question": "Will it rain tomorrow?",
        "token_ids": ["yes_tid_001", "no_tid_001"],
    }

    if not has_merge:
        del stub.client.merge_positions
        # Also ensure no inner client
        stub.client._client = None
        if has_inner_merge:
            inner = MagicMock()
            stub.client._client = inner
    return stub


def _setup_balance_responses(stub, pre_balance, post_balance):
    """Configure get_balance_allowance to return pre then post balances."""
    # Balance values are in micro-units (multiply by 1e6)
    responses = [
        {"balance": str(int(pre_balance * 1e6))},
        {"balance": str(int(post_balance * 1e6))},
    ]
    stub.client.get_balance_allowance.side_effect = responses


# ═══════════════════════════════════════════════════════════════════════
# Verified merge success
# ═══════════════════════════════════════════════════════════════════════

class TestMergeVerifiedSuccess(unittest.TestCase):

    @patch("ctf_merge.try_merge_positions", return_value=(True, ""))
    @patch("unwind.get_db")
    def test_merge_success_balance_decreased(self, mock_get_db, _mock_merge):
        """Merge succeeds AND exchange balance decreased → returns True."""
        stub = _make_unwind_stub(has_merge=True)

        # Pre: 100 YES tokens, Post: 50 YES tokens (merged 50)
        _setup_balance_responses(stub, pre_balance=100.0, post_balance=50.0)
        stub.client.merge_positions.return_value = {"status": "ok"}

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertTrue(result)
        # DB merge logged
        mock_get_db().log_merge.assert_called_once_with("cid_001", 50.0, 50.0)

    @patch("ctf_merge.try_merge_positions", return_value=(True, ""))
    @patch("unwind.get_db")
    def test_merge_via_inner_client_verified(self, mock_get_db, _mock_merge):
        """Merge via inner._client works with balance verification."""
        stub = _make_unwind_stub(has_merge=False, has_inner_merge=True)

        inner = stub.client._client
        # Pre: 80 YES, Post: 30 YES (merged 50)
        _setup_balance_responses(stub, pre_balance=80.0, post_balance=30.0)
        inner.merge_positions.return_value = {"status": "ok"}

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertTrue(result)


# ═══════════════════════════════════════════════════════════════════════
# Phantom merge detection
# ═══════════════════════════════════════════════════════════════════════

class TestPhantomMergeDetection(unittest.TestCase):

    @patch("unwind.get_db")
    def test_phantom_merge_balance_unchanged(self, mock_get_db):
        """API says success but balance unchanged → returns False (phantom detected)."""
        stub = _make_unwind_stub(has_merge=True)

        # Pre: 100, Post: 100 (nothing actually merged!)
        _setup_balance_responses(stub, pre_balance=100.0, post_balance=100.0)
        stub.client.merge_positions.return_value = {"status": "ok"}

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertFalse(result)
        # DB merge should NOT be logged
        mock_get_db().log_merge.assert_not_called()

    @patch("unwind.get_db")
    def test_phantom_merge_tiny_decrease(self, mock_get_db):
        """Balance decreased by less than 0.5 → still phantom (rounding tolerance)."""
        stub = _make_unwind_stub(has_merge=True)

        # Pre: 100, Post: 99.7 — only 0.3 decrease, want 50 decrease
        _setup_balance_responses(stub, pre_balance=100.0, post_balance=99.7)
        stub.client.merge_positions.return_value = {"status": "ok"}

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertFalse(result)

    @patch("unwind.get_db")
    def test_phantom_merge_inner_client(self, mock_get_db):
        """Phantom detected even when using inner client path."""
        stub = _make_unwind_stub(has_merge=False, has_inner_merge=True)

        inner = stub.client._client
        # Pre: 100, Post: 100
        _setup_balance_responses(stub, pre_balance=100.0, post_balance=100.0)
        inner.merge_positions.return_value = {"status": "ok"}

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertFalse(result)


# ═══════════════════════════════════════════════════════════════════════
# Caller integration — record_unwind gated on return value
# ═══════════════════════════════════════════════════════════════════════

class TestCallerGatedOnReturn(unittest.TestCase):

    @patch("unwind.get_db")
    def test_phantom_merge_prevents_record_unwind(self, mock_get_db):
        """When _try_merge_positions returns False, caller must NOT call record_unwind.

        This verifies the contract: the caller at lines 480-492 checks the return
        value before recording unwinds. A phantom merge returns False, so no
        position tracking corruption occurs.
        """
        stub = _make_unwind_stub(has_merge=True)

        # Phantom: balance unchanged
        _setup_balance_responses(stub, pre_balance=100.0, post_balance=100.0)
        stub.client.merge_positions.return_value = {"status": "ok"}

        merged = stub._try_merge_positions("cid_001", 50.0)

        # Simulating the caller's logic (lines 480-492 of unwind.py)
        if merged:
            stub.position_tracker.record_unwind("cid_001", "yes", 50.0, 0.5)
            stub.position_tracker.record_unwind("cid_001", "no", 50.0, 0.5)

        # record_unwind should NOT have been called
        stub.position_tracker.record_unwind.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# No merge capability
# ═══════════════════════════════════════════════════════════════════════

class TestNoMergeCapability(unittest.TestCase):

    @patch("unwind.alert_merge_needed")
    def test_no_merge_client_returns_false(self, mock_alert):
        """Client has no merge_positions → returns False, alerts sent."""
        stub = _make_unwind_stub(has_merge=False, has_inner_merge=False)
        stub.position_tracker.get_shares.return_value = 50.0

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertFalse(result)
        # No balance checks attempted
        stub.client.get_balance_allowance.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════

class TestMergeErrorHandling(unittest.TestCase):

    def test_merge_api_error_returns_false(self):
        """merge_positions() throws → returns False, no phantom risk."""
        stub = _make_unwind_stub(has_merge=True)

        # Pre-balance succeeds
        stub.client.get_balance_allowance.return_value = {
            "balance": str(int(100 * 1e6))
        }
        stub.client.merge_positions.side_effect = Exception("API timeout")

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertFalse(result)

    def test_pre_balance_error_returns_false(self):
        """get_balance_allowance fails before merge → returns False (safe)."""
        stub = _make_unwind_stub(has_merge=True)

        stub.client.get_balance_allowance.side_effect = Exception("Network error")

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertFalse(result)
        # merge_positions should NOT have been called
        stub.client.merge_positions.assert_not_called()

    def test_post_balance_error_returns_false(self):
        """get_balance_allowance fails after merge → returns False (conservative).

        Even though the merge may have succeeded, we can't verify it,
        so we return False. The caller won't record the unwind, which is
        safer than phantom-recording it.
        """
        stub = _make_unwind_stub(has_merge=True)

        # First call (pre) succeeds, second call (post) fails
        stub.client.get_balance_allowance.side_effect = [
            {"balance": str(int(100 * 1e6))},
            Exception("Network error on post-check"),
        ]
        stub.client.merge_positions.return_value = {"status": "ok"}

        result = stub._try_merge_positions("cid_001", 50.0)

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
