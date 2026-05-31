"""FX-086 (closes FX-064) — cold-start q_share is cfg-tunable.

The live EV gate's binding input for every unseen market was a HARDCODED module
constant simple_allocator.COLD_START_Q_SHARE=0.005, not tunable at runtime
(FX-046 says it is 24-94x off the true reward). estimate_q_share now reads
cfg("RF_COLD_START_Q_SHARE"); default unchanged at 0.005 (no behavior change on
its own) so it can be recalibrated via config_overrides.json once live reward
data lands. API and cumulative tiers still take precedence (cfg untouched there).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import simple_allocator as sa  # noqa: E402
from simple_allocator import COLD_START_Q_SHARE  # noqa: E402
from config import cfg as _real_cfg  # noqa: E402


def _bare_alloc():
    # estimate_q_share reads only its args + cfg — no self attrs — so __new__ is fine.
    return sa.SimpleAllocator.__new__(sa.SimpleAllocator)


class TestFX086ColdStartCfg(unittest.TestCase):

    def test_default_matches_constant_and_005(self):
        a = _bare_alloc()
        q, src = a.estimate_q_share("0xNEW", {}, {})
        self.assertEqual(src, "cold_start")
        self.assertEqual(q, 0.005)
        # Default mirror constant kept in sync (used by other tests' arithmetic).
        self.assertEqual(q, COLD_START_Q_SHARE)

    def test_cfg_override_changes_cold_start(self):
        a = _bare_alloc()

        def _cfg(key):
            return 0.02 if key == "RF_COLD_START_Q_SHARE" else _real_cfg(key)

        with patch.object(sa, "cfg", _cfg):
            q, src = a.estimate_q_share("0xNEW", {}, {})
        self.assertEqual(src, "cold_start")
        self.assertEqual(q, 0.02)

    def test_api_takes_precedence_over_cold_start(self):
        a = _bare_alloc()
        # Even with a non-default cold-start, an API value wins and cfg is not consulted.
        with patch.object(sa, "cfg", lambda k: 0.99):
            q, src = a.estimate_q_share("0xH", {"0xH": 0.30}, {})
        self.assertEqual((q, src), (0.30, "api"))

    def test_cumulative_takes_precedence_over_cold_start(self):
        a = _bare_alloc()
        with patch.object(sa, "cfg", lambda k: 0.99):
            q, src = a.estimate_q_share("0xC", {}, {"0xC": 0.10})
        self.assertEqual((q, src), (0.10, "cumulative"))


if __name__ == "__main__":
    unittest.main()
