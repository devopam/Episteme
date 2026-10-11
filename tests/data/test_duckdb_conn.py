import pytest

from episteme.data import duckdb_conn


def test_connect_applies_limit_and_temp_dir(tmp_path):
    tmp = tmp_path / "duck" / "tmp"
    con = duckdb_conn.connect(memory_limit="512MB", temp_dir=tmp)
    try:
        limit = con.execute("select current_setting('memory_limit')").fetchone()[0]
        assert limit.endswith("iB") or limit.endswith("B")
        assert limit.startswith(("488", "512"))  # 512MB reported as 488.2 MiB or 512.0 MB
        assert tmp.is_dir()
        assert con.execute("select current_setting('temp_directory')").fetchone()[0]
    finally:
        con.close()


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
