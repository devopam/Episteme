from pathlib import Path

import pytest

from episteme.data import duckdb_conn


def _temp_directory(con) -> Path:
    return Path(con.execute("select current_setting('temp_directory')").fetchone()[0])


def test_connect_applies_limit_and_temp_dir(tmp_path):
    root = tmp_path / "duck" / "tmp"
    con = duckdb_conn.connect(memory_limit="512MB", temp_dir=root)
    try:
        limit = con.execute("select current_setting('memory_limit')").fetchone()[0]
        assert limit.endswith("iB") or limit.endswith("B")
        assert limit.startswith(("488", "512"))  # 512MB reported as 488.2 MiB or 512.0 MB
        assert root.is_dir()
        spill = _temp_directory(con)
        assert spill.parent == root
        assert len(spill.name) == 32  # uuid4 hex
    finally:
        con.close()


@pytest.fixture
def settings_root(tmp_path, monkeypatch):
    """Point get_settings() at tmp_path (processed root) and a 256MB DuckDB limit.

    Clears the cache of the function duckdb_conn actually bound: other tests
    importlib.reload(episteme.config), leaving it a different object.
    """
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    monkeypatch.setenv("EPISTEME_DUCKDB_MEMORY_LIMIT", "256MB")
    # empty, not unset: an operator .env value would otherwise be loaded back
    monkeypatch.setenv("EPISTEME_DUCKDB_TEMP_DIR", "")
    duckdb_conn.get_settings.cache_clear()
    yield tmp_path
    duckdb_conn.get_settings.cache_clear()


def test_connect_defaults_come_from_settings(settings_root):
    con = duckdb_conn.connect()
    try:
        limit = con.execute("select current_setting('memory_limit')").fetchone()[0]
        assert limit.startswith(("244", "256"))  # 256MB reported as 244.1 MiB or 256.0 MB
        spill = _temp_directory(con)
        assert spill.parent == settings_root / "_ops" / "_duckdb_tmp"
    finally:
        con.close()


def test_each_connection_gets_its_own_spill_dir(settings_root):
    # A shared spill dir crashed a still-spilling connection when another closed.
    root = settings_root / "_ops" / "_duckdb_tmp"
    con1 = duckdb_conn.connect()
    con2 = duckdb_conn.connect()
    try:
        d1, d2 = _temp_directory(con1), _temp_directory(con2)
        assert d1 != d2
        assert d1.parent == root and d2.parent == root
    finally:
        con1.close()
        con2.close()


def test_spill_dir_removed_on_close(tmp_path):
    root = tmp_path / "spill"
    con = duckdb_conn.connect(memory_limit="16MB", temp_dir=root)
    spill = _temp_directory(con)
    try:
        con.execute("SET threads = 1")
        con.execute("select i, md5(i::varchar) h from range(1000000) t(i) order by h")
        first = con.fetchmany(10)
        assert len(first) == 10
        assert spill.is_dir() and any(spill.iterdir())  # it really spilled
    finally:
        con.close()
    assert not spill.exists()
    assert root.is_dir()


@pytest.mark.parametrize("bad", ["", "lots", "4 GB; drop", "1GB'; --", "-1GB", "4G"])
def test_connect_rejects_bad_limit(tmp_path, bad):
    with pytest.raises(ValueError):
        duckdb_conn.connect(memory_limit=bad, temp_dir=tmp_path)


def test_fetch_batches_sizes():
    import duckdb

    con = duckdb.connect()
    con.execute("select * from range(7)")
    batches = list(duckdb_conn.fetch_batches(con, 3))
    assert [len(b) for b in batches] == [3, 3, 1]
    assert [r[0] for b in batches for r in b] == list(range(7))


def test_fetch_batches_default_size():
    assert duckdb_conn.FETCH_BATCH_ROWS == 50_000


def test_connect_spills_under_the_configured_temp_dir(settings_root, tmp_path, monkeypatch):
    """EPISTEME_DUCKDB_TEMP_DIR moves the spill root (to a fast local disk)."""
    fast = tmp_path / "fast_disk"
    monkeypatch.setenv("EPISTEME_DUCKDB_TEMP_DIR", str(fast))
    duckdb_conn.get_settings.cache_clear()
    con = duckdb_conn.connect()
    try:
        assert _temp_directory(con).parent == fast
    finally:
        con.close()
