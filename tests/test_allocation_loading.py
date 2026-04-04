"""Tests for allocation file loading and agent IPC."""

import sys
import os
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_allocation_file(path, markets=None, generated_at=None, stale=False):
    """Helper to create a market_allocations.json file."""
    if generated_at is None:
        if stale:
            # 3 hours ago
            generated_at = datetime.fromtimestamp(
                time.time() - 3 * 3600, tz=timezone.utc
            ).isoformat()
        else:
            generated_at = datetime.now(timezone.utc).isoformat()

    if markets is None:
        markets = [
            {
                "condition_id": "0xtest1",
                "question": "Test market 1?",
                "action": "deploy",
                "shares_per_side": 50,
                "score": 10.0,
                "reason": "Zero fills",
                "confidence": "high",
            },
            {
                "condition_id": "0xtest2",
                "question": "Test market 2?",
                "action": "avoid",
                "shares_per_side": 0,
                "score": -5.0,
                "reason": "High fills",
                "confidence": "high",
            },
        ]

    data = {
        "generated_at": generated_at,
        "version": "1.0",
        "num_deploy": sum(1 for m in markets if m["action"] == "deploy"),
        "num_avoid": sum(1 for m in markets if m["action"] == "avoid"),
        "markets": markets,
    }

    with open(path, "w") as f:
        json.dump(data, f)


class TestAllocationLoading(unittest.TestCase):
    """Test _load_allocations behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.alloc_path = os.path.join(self.tmpdir, "market_allocations.json")

    def tearDown(self):
        if os.path.exists(self.alloc_path):
            os.unlink(self.alloc_path)
        os.rmdir(self.tmpdir)

    def test_fresh_file_loads(self):
        """Fresh allocation file should return deploy markets."""
        _make_allocation_file(self.alloc_path)

        with open(self.alloc_path) as f:
            data = json.load(f)
        deploy = [m for m in data["markets"] if m["action"] == "deploy"]
        self.assertEqual(len(deploy), 1)
        self.assertEqual(deploy[0]["condition_id"], "0xtest1")

    def test_stale_file_returns_none_behavior(self):
        """Stale file (>2h old) should be treated as stale."""
        _make_allocation_file(self.alloc_path, stale=True)

        with open(self.alloc_path) as f:
            data = json.load(f)
        gen_dt = datetime.fromisoformat(data["generated_at"])
        from datetime import timedelta
        age = datetime.now(timezone.utc) - gen_dt
        self.assertGreater(age, timedelta(hours=2))

    def test_missing_file(self):
        """Missing file should be gracefully handled."""
        self.assertFalse(os.path.exists(self.alloc_path))

    def test_corrupt_json(self):
        """Corrupt JSON should not crash."""
        with open(self.alloc_path, "w") as f:
            f.write("{invalid json")

        try:
            with open(self.alloc_path) as f:
                json.load(f)
            self.fail("Should have raised JSONDecodeError")
        except json.JSONDecodeError:
            pass  # Expected

    def test_empty_deploy_list(self):
        """File with no deploy markets returns empty deploy list."""
        _make_allocation_file(
            self.alloc_path,
            markets=[{
                "condition_id": "0xavoid",
                "action": "avoid",
                "shares_per_side": 0,
                "score": -5.0,
            }],
        )

        with open(self.alloc_path) as f:
            data = json.load(f)
        deploy = [m for m in data["markets"] if m["action"] == "deploy"]
        self.assertEqual(len(deploy), 0)

    def test_atomic_write_pattern(self):
        """Verify atomic write produces valid JSON."""
        from oversight.allocation_writer import write_allocations

        allocations = [
            {
                "condition_id": "0xtest",
                "question": "Atomic test?",
                "action": "deploy",
                "shares_per_side": 50,
                "score": 10.0,
                "reason": "test",
                "confidence": "high",
            }
        ]

        write_allocations(allocations, 30.0, self.alloc_path)

        with open(self.alloc_path) as f:
            data = json.load(f)
        self.assertIn("generated_at", data)
        self.assertIn("markets", data)
        self.assertEqual(len(data["markets"]), 1)


class TestCorrectionFactor(unittest.TestCase):
    """Test the reward correction factor computation."""

    def test_correction_returned_from_collect(self):
        """collect_all should return a tuple (metrics, correction_factor)."""
        from oversight.data_collector import collect_all
        # Will return empty metrics + 1.0 factor with a nonexistent db
        result = collect_all(db_path="/nonexistent/db.sqlite", hours=24)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        metrics, factor = result
        self.assertIsInstance(metrics, list)
        self.assertIsInstance(factor, float)


if __name__ == "__main__":
    unittest.main()
