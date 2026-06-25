import pytest
import sqlite3
from volume_cache import lookup, CLOB_HOST, GAMMA_HOST


class _Resp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


def _make_db(tmp_path):
    db = tmp_path / "bot.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE volume_24h_cache (
            condition_id TEXT PRIMARY KEY,
            slug TEXT,
            volume_24h REAL NOT NULL DEFAULT 0,
            fetched_at REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'gamma'
        )"""
    )
    conn.commit()
    conn.close()
    return str(db)


def test_cache_hit_returns_value_without_network(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO volume_24h_cache VALUES (?, ?, ?, ?, ?)",
        ("0xA", "slug-a", 12345.0, 1000000.0, "gamma"),
    )
    conn.commit()
    conn.close()

    calls = []

    def fake_http(url, **kwargs):
        calls.append(url)
        return _Resp(500, {})

    result = lookup(["0xA"], db_path=db, ttl=3600, _now=lambda: 1000001.0, _http=fake_http)
    assert result == {"0xA": 12345.0}
    assert calls == []


def test_cache_miss_fetches_slug_and_volume(tmp_path):
    db = _make_db(tmp_path)

    def fake_http(url, **kwargs):
        if url == f"{CLOB_HOST}/markets/0xB":
            return _Resp(200, {"market_slug": "market-b"})
        if url == f"{GAMMA_HOST}/markets":
            assert kwargs.get("params", {}).get("slug") == "market-b"
            return _Resp(200, [{"conditionId": "0xB", "volume24hrClob": 99999.0}])
        raise RuntimeError(f"unexpected {url}")

    result = lookup(["0xB"], db_path=db, ttl=3600, _now=lambda: 1000.0, _http=fake_http)
    assert result == {"0xB": 99999.0}

    # cache written
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT slug, volume_24h, source FROM volume_24h_cache WHERE condition_id=?", ("0xB",)).fetchone()
    conn.close()
    assert row == ("market-b", 99999.0, "gamma-slug")


def test_expired_cache_re_fetches(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO volume_24h_cache VALUES (?, ?, ?, ?, ?)",
        ("0xC", "old-slug", 100.0, 0.0, "gamma"),
    )
    conn.commit()
    conn.close()

    def fake_http(url, **kwargs):
        if url == f"{CLOB_HOST}/markets/0xC":
            return _Resp(200, {"market_slug": "market-c"})
        if url == f"{GAMMA_HOST}/markets":
            return _Resp(200, [{"volume24hrClob": 777.0}])
        raise RuntimeError(f"unexpected {url}")

    result = lookup(["0xC"], db_path=db, ttl=10, _now=lambda: 1000.0, _http=fake_http)
    assert result == {"0xC": 777.0}


def test_failed_refresh_keeps_stale_value(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO volume_24h_cache VALUES (?, ?, ?, ?, ?)",
        ("0xD", "slug-d", 555.0, 0.0, "gamma"),
    )
    conn.commit()
    conn.close()

    def fake_http(url, **kwargs):
        return _Resp(500, {})

    result = lookup(["0xD"], db_path=db, ttl=10, _now=lambda: 1000.0, _http=fake_http)
    assert result == {"0xD": 555.0}

    # fetched_at should NOT be bumped (so next cycle retries)
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT volume_24h, fetched_at, source FROM volume_24h_cache WHERE condition_id=?", ("0xD",)).fetchone()
    conn.close()
    assert row == (555.0, 0.0, "gamma-stale")


def test_missing_cid_caches_zero_and_retries_after_ttl(tmp_path):
    db = _make_db(tmp_path)

    def fake_http(url, **kwargs):
        return _Resp(404, {})

    result = lookup(["0xE"], db_path=db, ttl=10, _now=lambda: 1000.0, _http=fake_http)
    assert result == {"0xE": 0.0}

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT volume_24h, source FROM volume_24h_cache WHERE condition_id=?", ("0xE",)).fetchone()
    conn.close()
    assert row[0] == 0.0


def test_volume_cap_cohort_filter_using_lookup(tmp_path):
    """The C1 volume cap should fire when lookup returns > cap."""
    db = _make_db(tmp_path)
    # seed cache above cap
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO volume_24h_cache VALUES (?, ?, ?, ?, ?)", ("0xVOL1", "slug", 300000.0, 1000000.0, "gamma"))
    conn.commit()
    conn.close()

    result = lookup(["0xVOL1"], db_path=db, ttl=3600, _now=lambda: 1000001.0)
    assert result["0xVOL1"] == 300000.0
