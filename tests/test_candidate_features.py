"""Tests for A3 candidate_features survivorship log.

The load-bearing proof: the capture is NON-BEHAVIORAL — compute()'s `deploys` (and `avoids`) are
byte-identical with the flag ON vs OFF (test_output_byte_identical). Plus fail-open (a capture error
never breaks compute), capture-record correctness, and the isolated-DB log module. No network, no live
state — every external call is stubbed and the DB is :memory:.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import config
import simple_allocator as sa
from simple_allocator import SimpleAllocator, CandidateMarket

_real_cfg = config.cfg


def _cfg_with_flag(value):
    """A cfg() that forces RF_CANDIDATE_FEATURE_LOG_ENABLED and delegates everything else to real cfg."""
    def _c(name):
        if name == "RF_CANDIDATE_FEATURE_LOG_ENABLED":
            return value
        return _real_cfg(name)
    return _c


def _make_allocator(now=1700000000):
    return SimpleAllocator(
        db_path=":memory:", wallet_address="0xWALLET", funder="0xFUNDER",
        api_key="key", api_secret="MTIzNDU2Nzg5MDEyMzQ1Ng==", api_passphrase="pass",
        _now=lambda: now,
        _http=lambda *a, **k: SimpleNamespace(status_code=500, text="", json=lambda: {}),
    )


def _cand(cid, daily_rate=100, min_size=20):
    return CandidateMarket(condition_id=cid, yes_tid=f"y{cid[:6]}", no_tid=f"n{cid[:6]}",
                           daily_rate=daily_rate, max_spread=4.5, min_size=min_size)


def _mixed():
    # 3 that deploy (small min_size) + 2 that avoid via the EV gate (large min_size -> high fill cost).
    return ([_cand(f"0x{i:040x}", min_size=20) for i in range(3)] +
            [_cand(f"0x{100 + i:040x}", min_size=500) for i in range(2)])


_KW = dict(wallet_usd=1000.0, wallet_peak_usd=1000.0, wallet_24h_ago_usd=1000.0, realized_loss_24h=0.0)


def _deploy_sig(result):
    return [(m.condition_id, m.target_shares, m.target_capital) for m in result.deploys]


def test_output_byte_identical(monkeypatch):
    """PROOF OF 0 BEHAVIORAL AXES: deploys + avoids are identical with the flag OFF vs ON."""
    a = _make_allocator()
    monkeypatch.setattr(config, "cfg", _cfg_with_flag(False))
    off = a.compute(**_KW, markets=_mixed())
    monkeypatch.setattr(config, "cfg", _cfg_with_flag(True))
    on = a.compute(**_KW, markets=_mixed())
    assert _deploy_sig(off) == _deploy_sig(on)
    assert {m.condition_id for m in off.avoids} == {m.condition_id for m in on.avoids}
    assert off.candidate_features == []                    # OFF -> nothing captured
    assert len(on.candidate_features) >= len(on.deploys)   # ON -> eligible set captured


def test_capture_records(monkeypatch):
    a = _make_allocator()
    monkeypatch.setattr(config, "cfg", _cfg_with_flag(True))
    r = a.compute(**_KW, markets=_mixed())
    actions = [rec["action"] for rec in r.candidate_features]
    assert "deploy" in actions and "avoid" in actions        # both sides of the decision boundary logged
    cids = {rec["condition_id"] for rec in r.candidate_features}
    assert {m.condition_id for m in r.deploys} <= cids        # every deploy is logged
    for rec in r.candidate_features:
        assert set(rec) >= {"condition_id", "cohort", "action", "daily_rate", "expected_daily_reward"}


def test_capture_fail_open(monkeypatch):
    """A capture error must NEVER break compute() — it fails open to an empty list."""
    a = _make_allocator()
    monkeypatch.setattr(config, "cfg", _cfg_with_flag(True))

    def _boom(*a, **k):
        raise RuntimeError("capture boom")

    monkeypatch.setattr(a, "_ab_cohort", _boom)   # only called inside the capture block (A/B off)
    r = a.compute(**_KW, markets=_mixed())
    assert r.candidate_features == []              # capture failed open
    assert len(r.deploys) >= 1                      # decision path unaffected


def test_log_module(tmp_path):
    import candidate_features_log as cfl
    db = str(tmp_path / "cf.db")
    rec = {"condition_id": "0xA", "cohort": 0, "action": "deploy", "reason": "",
           "daily_rate": 100.0, "max_spread": 4.5, "min_size": 20, "midpoint_guess": 0.5,
           "expected_q_share": 0.005, "q_share_source": "cold_start", "expected_daily_reward": 0.5,
           "target_shares": 22, "target_capital": 22.0, "end_date_iso": "", "game_start_time": "",
           "question": "Will X?"}
    assert cfl.append([rec], db_path=db) == 1
    assert cfl.append([], db_path=db) == 0           # empty -> no-op
    assert cfl.append([rec], db_path=db) == 1         # idempotent schema, second append
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM candidate_features").fetchone()[0] == 2
    finally:
        conn.close()


def test_log_result_glue(tmp_path):
    """The oversight wiring: log_result persists candidate_features, no-ops on empty/missing."""
    import candidate_features_log as cfl
    db = str(tmp_path / "cf.db")
    rec = {"condition_id": "0xA", "cohort": 0, "action": "deploy", "reason": "", "daily_rate": 100.0,
           "max_spread": 4.5, "min_size": 20, "midpoint_guess": 0.5, "expected_q_share": 0.005,
           "q_share_source": "cold_start", "expected_daily_reward": 0.5, "target_shares": 22,
           "target_capital": 22.0, "end_date_iso": "", "game_start_time": "", "question": "Q?"}
    assert cfl.log_result(SimpleNamespace(candidate_features=[rec]), db_path=db) == 1   # writes
    assert cfl.log_result(SimpleNamespace(candidate_features=[]), db_path=db) == 0       # empty -> no-op
    assert cfl.log_result(SimpleNamespace(), db_path=db) == 0                            # missing attr -> no-op
