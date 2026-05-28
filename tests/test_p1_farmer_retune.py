"""Adversarial audit — P1 of 9/10 plan (FX-058 + FX-043).

Two changes under test:

  1. Kill-threshold retune (FX-058):
     - MAX_NOTIONAL_RATIO 2.0 → cfg-driven (default 5.0)
     - HARD_NOTIONAL_RATIO 2.5 → cfg-driven (default 8.0)
     - NEW rapid-growth kill: notional_ratio burst > 5× over 5 min → kill
     The static thresholds intentionally permit healthy overcommit (Rule 2,
     3-8× wallet notional). The acceleration kill catches misconfigured
     allocators without false-firing on normal overcommit.

  2. _total_capital metadata stamping (FX-043):
     - simple_allocator stamps `_total_capital` at top-level metadata
     - farmer reader prefers top-level, falls back to deploy row, then
       avoid row, then None. Fixes the silent fail-open on 0-deploy cycles.

Attack families:

  AT-A  cfg-driven thresholds (retune at runtime, no code redeploy)
  AT-B  rapid-growth kill (acceleration detection)
  AT-C  FX-043 fallback chain (metadata → deploy → avoid → None)
  AT-D  Backward compat (legacy alloc.json without metadata stamp)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── SDK shim (consistent with other audit test files) ──────────────────────

import types


class _PassthroughDataclass:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _EnumLike:
    COLLATERAL = "COLLATERAL"
    CONDITIONAL = "CONDITIONAL"


def _install_passthrough_clob_shim() -> None:
    stale = [
        k for k in list(sys.modules)
        if (k == "py_clob_client_v2" or k.startswith("py_clob_client_v2."))
        and isinstance(sys.modules[k], MagicMock)
    ]
    for k in stale:
        del sys.modules[k]
    try:
        import py_clob_client_v2.clob_types  # noqa: F401
        import py_clob_client_v2.order_builder.constants  # noqa: F401
        return
    except ImportError:
        pass
    mock_clob = MagicMock()
    clob_types = types.ModuleType("py_clob_client_v2.clob_types")
    clob_types.BalanceAllowanceParams = _PassthroughDataclass
    clob_types.OrderPayload = _PassthroughDataclass
    clob_types.OrderArgs = _PassthroughDataclass
    clob_types.AssetType = _EnumLike
    order_builder = types.ModuleType("py_clob_client_v2.order_builder")
    constants_mod = types.ModuleType("py_clob_client_v2.order_builder.constants")
    constants_mod.BUY = "BUY"
    constants_mod.SELL = "SELL"
    order_builder.constants = constants_mod
    sys.modules["py_clob_client_v2"] = mock_clob
    sys.modules["py_clob_client_v2.clob_types"] = clob_types
    sys.modules["py_clob_client_v2.order_builder"] = order_builder
    sys.modules["py_clob_client_v2.order_builder.constants"] = constants_mod


_install_passthrough_clob_shim()


import reward_farmer
from simple_allocator import (
    SimpleAllocator,
    AllocationResult,
    CandidateMarket,
)


# ════════════════════════════════════════════════════════════════════════════
# AT-A — cfg-driven thresholds
# ════════════════════════════════════════════════════════════════════════════


class TestAT_A_CfgThresholds(unittest.TestCase):
    """The four notional thresholds must be cfg-driven so operators can
    retune via config_overrides.json without a code redeploy."""

    def test_AT_A1_defaults_match_overcommit_design(self):
        """Defaults must support Ground Rule 2's 3-8× wallet notional band."""
        self.assertGreaterEqual(reward_farmer.MAX_NOTIONAL_RATIO(), 3.0,
                                "MAX_NOTIONAL_RATIO must permit ≥3× overcommit")
        self.assertGreaterEqual(reward_farmer.HARD_NOTIONAL_RATIO(),
                                reward_farmer.MAX_NOTIONAL_RATIO(),
                                "HARD must be ≥ MAX (hard cap ≥ soft cap)")
        self.assertLessEqual(reward_farmer.HARD_NOTIONAL_RATIO(), 10.0,
                             "HARD must cap above design point (3-8×) but not absurd")

    def test_AT_A2_config_override_retunes_without_code_change(self):
        """Patching cfg() must change the active threshold immediately."""
        original = reward_farmer.MAX_NOTIONAL_RATIO()
        with patch("reward_farmer.cfg") as mock_cfg:
            mock_cfg.side_effect = lambda k: 7.5 if k == "RF_MAX_NOTIONAL_RATIO" else 42
            self.assertEqual(7.5, reward_farmer.MAX_NOTIONAL_RATIO())
        # Restore: outside the patch, cfg() returns the real value
        self.assertEqual(original, reward_farmer.MAX_NOTIONAL_RATIO())

    def test_AT_A3_rapid_growth_kill_disabled_when_zero(self):
        """Setting RF_RAPID_GROWTH_KILL_RATIO=0 must disable the kill
        entirely (escape hatch for operators who want raw-threshold semantics).
        """
        rf = _make_farmer_stub()
        with patch("reward_farmer.cfg") as mock_cfg:
            mock_cfg.side_effect = lambda k: (
                0.0 if k == "RF_RAPID_GROWTH_KILL_RATIO"
                else 300.0 if k == "RF_RAPID_GROWTH_WINDOW_SEC"
                else 5.0
            )
            # Inject huge burst: 1× → 100× would normally trip
            for i, v in enumerate([1.0, 100.0]):
                rf._notional_ratio_samples.append((time.time() + i, v))
            kill, mult = rf._guardrail_rapid_notional_growth(100.0)
        self.assertFalse(kill, "kill_ratio=0 must disable the rapid-growth kill")


# ════════════════════════════════════════════════════════════════════════════
# AT-B — Rapid-growth kill
# ════════════════════════════════════════════════════════════════════════════


def _make_farmer_stub() -> reward_farmer.RewardFarmer:
    """Build a RewardFarmer with all heavyweight init bypassed —
    enough for the guardrail helpers to be exercised in isolation."""
    rf = reward_farmer.RewardFarmer.__new__(reward_farmer.RewardFarmer)
    rf.mode = "LIVE"
    rf.dry_run = False
    rf.markets = {}
    rf._consecutive_hard_notional_breach_cycles = 0
    rf._notional_ratio_samples = deque()
    rf.cycle_count = 0
    return rf


class TestAT_B_RapidGrowthKill(unittest.TestCase):
    """Acceleration-based kill: notional_ratio bursting >X× in <Y sec → kill."""

    def test_AT_B1_burst_5x_in_window_kills(self):
        """notional_ratio 1.0 → 6.0 (6× burst) in 60s → kill fires.
        Default kill_ratio=5.0 so 6× > 5× → True.
        """
        rf = _make_farmer_stub()
        now = time.time()
        rf._notional_ratio_samples.append((now - 60, 1.0))
        # Inject burst — tick() appends current and re-evaluates
        kill, mult = rf._guardrail_rapid_notional_growth(6.0)
        self.assertTrue(kill, f"6× burst must trigger kill (observed mult={mult})")
        self.assertGreater(mult, 5.0)

    def test_AT_B2_gradual_growth_no_kill(self):
        """Slow climb 3.0 → 4.5 over the window stays under 5×. No kill."""
        rf = _make_farmer_stub()
        now = time.time()
        rf._notional_ratio_samples.append((now - 200, 3.0))
        rf._notional_ratio_samples.append((now - 100, 3.5))
        kill, mult = rf._guardrail_rapid_notional_growth(4.5)
        self.assertFalse(kill, f"gradual 1.5× growth must not kill (observed={mult})")

    def test_AT_B3_cold_start_single_sample_no_kill(self):
        """First cycle ever — only one sample in deque. No baseline → no kill."""
        rf = _make_farmer_stub()
        # rf._notional_ratio_samples starts empty
        kill, mult = rf._guardrail_rapid_notional_growth(50.0)
        self.assertFalse(kill, "cold-start single sample must not kill")
        self.assertIsNone(mult)

    def test_AT_B4_old_samples_evict_from_window(self):
        """Samples older than window are evicted. A burst that's now stale
        must not block re-evaluation of fresh ratios.
        """
        rf = _make_farmer_stub()
        now = time.time()
        # Stale samples (older than 300s default) — must be evicted
        rf._notional_ratio_samples.append((now - 1000, 1.0))
        rf._notional_ratio_samples.append((now - 800, 50.0))
        kill, mult = rf._guardrail_rapid_notional_growth(5.0)
        # All prior samples should have been evicted; only `5.0` remains.
        # < 2 samples → no kill possible
        self.assertFalse(kill)
        # Confirm deque was actually evicted (not just kill returned False)
        self.assertEqual(1, len(rf._notional_ratio_samples))

    def test_AT_B5_missing_signal_no_kill_no_deque_mutation(self):
        """If notional_ratio is None (e.g., DB hiccup), the deque must NOT
        be appended to AND no kill fires. This prevents a transient missing
        signal from resetting the burst-detection window OR triggering a
        false alarm.
        """
        rf = _make_farmer_stub()
        now = time.time()
        rf._notional_ratio_samples.append((now - 60, 5.0))
        prior_len = len(rf._notional_ratio_samples)
        kill, mult = rf._guardrail_rapid_notional_growth(None)
        self.assertFalse(kill)
        self.assertEqual(prior_len, len(rf._notional_ratio_samples),
                         "missing signal must not append to deque")

    def test_AT_B6_div_zero_guarded_at_lo_near_zero(self):
        """If the min sample is ~0 (cold start with 0 capital briefly),
        the division must not blow up. Clamp lo to 0.0001."""
        rf = _make_farmer_stub()
        now = time.time()
        rf._notional_ratio_samples.append((now - 60, 0.0))
        # 0.0 / clamped 0.0001 → 100× — this WILL kill (correct behavior),
        # but must not raise ZeroDivisionError
        try:
            kill, mult = rf._guardrail_rapid_notional_growth(0.5)
            # Either kill or no-kill is acceptable — what matters is no crash
            self.assertIsInstance(mult, float)
        except ZeroDivisionError:
            self.fail("rapid-growth kill must guard against div-by-zero on cold-start")


# ════════════════════════════════════════════════════════════════════════════
# AT-C — FX-043 _total_capital fallback chain
# ════════════════════════════════════════════════════════════════════════════


def _write_alloc(path: str, payload: dict) -> None:
    with open(path, "w") as f:
        json.dump(payload, f)


class TestAT_C_FX043_FallbackChain(unittest.TestCase):
    """Resolution order: metadata → deploy row → avoid row → None."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.alloc_path = os.path.join(self.tmpdir, "market_allocations.json")
        self.rf = _make_farmer_stub()
        # The reader uses __file__ to locate the alloc — patch the dirname
        self.patcher = patch.object(
            reward_farmer.os.path, "dirname",
            return_value=self.tmpdir,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for f in os.listdir(self.tmpdir):
            os.unlink(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def test_AT_C1_metadata_stamp_preferred(self):
        """Top-level _total_capital is the primary source."""
        _write_alloc(self.alloc_path, {
            "_total_capital": 1234.56,
            "markets": [],
        })
        self.assertEqual(1234.56, self.rf._guardrail_total_capital_from_alloc())

    def test_AT_C2_zero_deploy_alloc_returns_metadata_capital(self):
        """The FX-043 reproducer: 0 deploy rows but alloc was produced
        cleanly. Pre-FX-043 this returned None → fail-open. Post-FX-043
        the metadata stamp covers it.
        """
        _write_alloc(self.alloc_path, {
            "_total_capital": 1200.00,
            "markets": [
                # only avoid rows — happens during reallocation transitions
                {"condition_id": "0xA", "action": "avoid", "_total_capital": 1200.00},
                {"condition_id": "0xB", "action": "avoid", "_total_capital": 1200.00},
            ],
        })
        self.assertEqual(1200.00, self.rf._guardrail_total_capital_from_alloc())

    def test_AT_C3_legacy_alloc_no_metadata_falls_back_to_deploy_row(self):
        """Backward compat: legacy alloc.json with no metadata stamp but
        deploy rows still resolves correctly via the deploy-row path."""
        _write_alloc(self.alloc_path, {
            "markets": [
                {"condition_id": "0xA", "action": "deploy", "_total_capital": 999.99},
            ],
        })
        self.assertEqual(999.99, self.rf._guardrail_total_capital_from_alloc())

    def test_AT_C4_zero_deploy_no_metadata_falls_back_to_avoid_row(self):
        """No metadata stamp AND no deploy rows — fall back to avoid row.
        SimpleAllocator stamps _total_capital on every row, so this is
        always present in practice."""
        _write_alloc(self.alloc_path, {
            "markets": [
                {"condition_id": "0xA", "action": "avoid", "_total_capital": 500.00},
            ],
        })
        self.assertEqual(500.00, self.rf._guardrail_total_capital_from_alloc())

    def test_AT_C5_completely_empty_alloc_returns_none_with_warning(self):
        """Edge case: no metadata, no rows. Must return None (fail-open)
        AND emit a [GUARDRAIL_WARNING] for operator visibility."""
        import logging
        _write_alloc(self.alloc_path, {"markets": []})
        with self.assertLogs("reward_farmer", level="WARNING") as cm:
            result = self.rf._guardrail_total_capital_from_alloc()
        self.assertIsNone(result)
        self.assertTrue(any("missing_signal=total_capital" in msg for msg in cm.output))

    def test_AT_C6_alloc_file_absent_returns_none_with_warning(self):
        """Genuinely missing file (e.g., before first allocator cycle)."""
        # Don't create the file
        import logging
        with self.assertLogs("reward_farmer", level="WARNING") as cm:
            result = self.rf._guardrail_total_capital_from_alloc()
        self.assertIsNone(result)
        self.assertTrue(any("alloc file not found" in msg for msg in cm.output))


# ════════════════════════════════════════════════════════════════════════════
# AT-D — End-to-end writer + reader round trip
# ════════════════════════════════════════════════════════════════════════════


class TestAT_D_E2E_AllocatorAndReader(unittest.TestCase):
    """The SimpleAllocator → alloc.json → farmer-reader round trip must
    preserve _total_capital even on 0-deploy cycles."""

    def test_AT_D1_simple_allocator_zero_deploy_produces_readable_capital(self):
        """SimpleAllocator with 0 eligible markets writes _total_capital
        to the file. Farmer reader sees it via the metadata-stamp path.
        """
        tmpdir = tempfile.mkdtemp()
        alloc_path = os.path.join(tmpdir, "market_allocations.json")
        try:
            # Build a 0-deploy AllocationResult (kill_switch=True forces 0 deploys)
            result = AllocationResult(
                deploys=[], avoids=[],
                total_capital=1200.0, capital_deployed=0,
                expected_total_reward=0,
                kill_switch=True, kill_reason="test",
            )
            allocator = SimpleAllocator(
                db_path=":memory:", wallet_address="0xW", funder="0xF",
                api_key="", api_secret="", api_passphrase="",
            )
            allocator.write_allocation_json(result, output_path=alloc_path)

            # Verify the file has the metadata stamp
            with open(alloc_path) as f:
                data = json.load(f)
            self.assertIn("_total_capital", data)
            self.assertEqual(1200.0, data["_total_capital"])

            # Verify farmer reader sees it
            rf = _make_farmer_stub()
            with patch.object(reward_farmer.os.path, "dirname",
                              return_value=tmpdir):
                cap = rf._guardrail_total_capital_from_alloc()
            self.assertEqual(1200.0, cap,
                             "FX-043: 0-deploy alloc must still give farmer a capital signal")
        finally:
            for f in os.listdir(tmpdir):
                os.unlink(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)


if __name__ == "__main__":
    unittest.main()
