"""FX-094 — adversarial tests for ctf_merge.try_merge_positions."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctf_merge  # noqa: E402


class TestCtfMerge(unittest.TestCase):

    def test_merge_unavailable_without_builder_creds(self):
        with patch.dict(os.environ, {}, clear=True):
            ok, reason = ctf_merge.try_merge_positions(
                MagicMock(), condition_id="0xabc", amount=10.0, yes_tid="tid1",
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "merge_unavailable")

    @patch.object(ctf_merge, "_make_poly_service")
    def test_phantom_merge_rejected(self, mock_service_factory):
        svc = MagicMock()
        svc.merge.return_value = {"status": "ok"}
        mock_service_factory.return_value = svc

        balances = [100.0, 100.0]  # unchanged

        def _bal():
            return balances.pop(0) if balances else 100.0

        ok, reason = ctf_merge.try_merge_positions(
            MagicMock(),
            condition_id="0xabc",
            amount=50.0,
            yes_tid="tid1",
            verify_balance_fn=_bal,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "phantom_merge")

    @patch.object(ctf_merge, "_make_poly_service")
    def test_success_with_balance_drop(self, mock_service_factory):
        svc = MagicMock()
        svc.merge.return_value = {"status": "ok"}
        mock_service_factory.return_value = svc

        seq = [100.0, 50.0]

        def _bal():
            return seq.pop(0)

        ok, reason = ctf_merge.try_merge_positions(
            MagicMock(),
            condition_id="0xabc",
            amount=50.0,
            yes_tid="tid1",
            verify_balance_fn=_bal,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    @patch.object(ctf_merge, "_make_poly_service")
    def test_merge_exception_returns_false(self, mock_service_factory):
        svc = MagicMock()
        svc.merge.side_effect = RuntimeError("relayer timeout")
        mock_service_factory.return_value = svc

        ok, reason = ctf_merge.try_merge_positions(
            MagicMock(), condition_id="0xabc", amount=10.0, yes_tid="tid1",
        )
        self.assertFalse(ok)
        self.assertIn("merge_exception", reason)

    def test_amount_below_one_rejected(self):
        ok, reason = ctf_merge.try_merge_positions(
            MagicMock(), condition_id="0xabc", amount=0.5, yes_tid="tid1",
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "amount < 1")


if __name__ == "__main__":
    unittest.main()
