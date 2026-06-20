"""Tests for the orphan-leak backstop: reward_farmer._reconcile_share_drift.

Pure-function tests via a stub `self` (no RewardFarmer construction, no network). Covers: flag-off
no-op (byte-identical), reconcile+dump on persisted drift using on-chain avgPrice, the debounce,
below-threshold no-op, skip-when-dumping, unliquidatable skip, avgPrice->vwap fallback, and that an
untracked cid is ignored (that's the cid-level path's job).
"""
from __future__ import annotations

from types import SimpleNamespace

import config
import reward_farmer as rf

CID = "0xba8526"


def _cfg(enabled=True, min_shares=1.0, debounce=1):
    def _c(name):
        return {
            "RF_SYNC_SHARE_DRIFT_ENABLED": enabled,
            "RF_ORPHAN_DRIFT_MIN_SHARES": min_shares,
            "RF_ORPHAN_DRIFT_DEBOUNCE_SYNCS": debounce,
        }.get(name, getattr(config, name, None))
    return _c


class _Pos:
    def __init__(self, shares):
        self._s = dict(shares)
        self.sets = []

    def get_shares(self, cid, side):
        return self._s.get((cid, side), 0.0)

    def set_shares(self, cid, side, n, avg_price=None):
        self._s[(cid, side)] = n
        self.sets.append((cid, side, n, avg_price))


class _Db:
    def __init__(self, unliq=(), vwap=(0, 0.0)):
        self._unliq = set(unliq)
        self._vwap = vwap

    def is_unliquidatable(self, cid):
        return cid in self._unliq

    def fills_vwap(self, cid, side):
        return self._vwap


class _Dump:
    def __init__(self):
        self.dumped = []

    def dump_position(self, ms, side, shares):
        self.dumped.append((ms.cid, side, shares))


def _ms(cid, dumping_side=None):
    do = {"yes": None, "no": None}
    ds = {"yes": None, "no": None}
    if dumping_side:
        do[dumping_side] = "active-oid"
    return SimpleNamespace(cid=cid, dump_orders=do, dump_state=ds)


def _fake(tracked, markets, unliq=(), vwap=(0, 0.0)):
    return SimpleNamespace(
        _orphan_drift_pending={},
        db=_Db(unliq, vwap), markets=markets,
        positions=_Pos(tracked), dump_mgr=_Dump(),
    )


def _exch(cid, side, shares, avg=0.49):
    return {cid: {side: {"shares": shares, "avg_price": avg, "question": "Q?", "token_id": "t"}}}


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.setattr(rf, "cfg", _cfg(enabled=False))
    f = _fake({(CID, "no"): 50.0}, {CID: _ms(CID)})
    assert rf.RewardFarmer._reconcile_share_drift(f, _exch(CID, "no", 220.0)) == 0
    assert f.positions.sets == [] and f.dump_mgr.dumped == []


def test_reconcile_and_dump_uses_onchain_avg(monkeypatch):
    monkeypatch.setattr(rf, "cfg", _cfg(debounce=1))
    f = _fake({(CID, "no"): 50.0}, {CID: _ms(CID)})
    assert rf.RewardFarmer._reconcile_share_drift(f, _exch(CID, "no", 220.0, avg=0.49)) == 1
    assert f.positions.sets == [(CID, "no", 220.0, 0.49)]   # reconciled to on-chain truth + on-chain avg
    assert f.dump_mgr.dumped == [(CID, "no", 220.0)]


def test_below_threshold_noop(monkeypatch):
    monkeypatch.setattr(rf, "cfg", _cfg(min_shares=1.0, debounce=1))
    f = _fake({(CID, "no"): 50.0}, {CID: _ms(CID)})
    assert rf.RewardFarmer._reconcile_share_drift(f, _exch(CID, "no", 50.5)) == 0   # excess 0.5 < 1.0
    assert f.dump_mgr.dumped == []


def test_debounce_requires_persistence(monkeypatch):
    monkeypatch.setattr(rf, "cfg", _cfg(debounce=2))
    f = _fake({(CID, "no"): 50.0}, {CID: _ms(CID)})
    exch = _exch(CID, "no", 220.0)
    assert rf.RewardFarmer._reconcile_share_drift(f, exch) == 0    # 1st sync: pending only
    assert f.dump_mgr.dumped == []
    assert rf.RewardFarmer._reconcile_share_drift(f, exch) == 1    # 2nd sync: persisted -> acts
    assert f.dump_mgr.dumped == [(CID, "no", 220.0)]


def test_skip_when_dump_in_flight(monkeypatch):
    monkeypatch.setattr(rf, "cfg", _cfg(debounce=1))
    f = _fake({(CID, "no"): 50.0}, {CID: _ms(CID, dumping_side="no")})
    assert rf.RewardFarmer._reconcile_share_drift(f, _exch(CID, "no", 220.0)) == 0
    assert f.dump_mgr.dumped == []


def test_unliquidatable_skipped(monkeypatch):
    monkeypatch.setattr(rf, "cfg", _cfg(debounce=1))
    f = _fake({(CID, "no"): 50.0}, {CID: _ms(CID)}, unliq=(CID,))
    assert rf.RewardFarmer._reconcile_share_drift(f, _exch(CID, "no", 220.0)) == 0
    assert f.dump_mgr.dumped == []


def test_avg_falls_back_to_vwap_when_onchain_zero(monkeypatch):
    monkeypatch.setattr(rf, "cfg", _cfg(debounce=1))
    f = _fake({(CID, "no"): 50.0}, {CID: _ms(CID)}, vwap=(170, 0.55))
    rf.RewardFarmer._reconcile_share_drift(f, _exch(CID, "no", 220.0, avg=0.0))
    assert f.positions.sets == [(CID, "no", 220.0, 0.55)]   # on-chain avg 0 -> fills_vwap 0.55


def test_untracked_cid_ignored(monkeypatch):
    # share-drift handles only TRACKED cids; an entirely-untracked cid is the cid-level path's job
    monkeypatch.setattr(rf, "cfg", _cfg(debounce=1))
    f = _fake({}, {})
    assert rf.RewardFarmer._reconcile_share_drift(f, _exch(CID, "no", 220.0)) == 0
    assert f.dump_mgr.dumped == []
