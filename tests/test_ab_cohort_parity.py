"""Parity guard: ab.cohort.cohort MUST byte-match simple_allocator._ab_cohort.

If the live allocator's cohort function ever drifts from the offline analyzer's, every A/B cohort
comparison silently becomes wrong. This test fails loudly on any divergence.
"""
import config
from ab.cohort import cohort


def _alloc():
    from simple_allocator import SimpleAllocator
    return SimpleAllocator(db_path=":memory:", wallet_address="0x0", funder="0x0",
                           api_key="", api_secret="", api_passphrase="")


def test_cohort_parity_matches_allocator(monkeypatch):
    a = _alloc()
    cids = [("0x%040x" % i) for i in range(300)] + ["", "abc", "0xDEADBEEFcafe", "weather-nyc-rain"]
    for n in (2, 3, 4, 5):
        # _ab_cohort reads cfg("RF_AB_COHORT_COUNT") via `from config import cfg` at call time.
        monkeypatch.setattr(config, "cfg", lambda k, _n=n: _n if k == "RF_AB_COHORT_COUNT" else None)
        for c in cids:
            assert cohort(c, n) == a._ab_cohort(c), (c, n)


def test_cohort_degenerate():
    assert cohort("x", 1) == 0
    assert cohort("x", 0) == 0
    assert cohort("x", None) == 0
