"""Non-pg checks of postgres_loader's streaming readers and argument guards."""

from pathlib import Path

import pytest

from episteme.data import postgres_loader
from episteme.data.article_schema import empty_article_row, finalize_row
from episteme.data.staging_writer import write_rows


def _shard(tmp_path: Path, n: int, *, prefer_parquet: bool = True) -> Path:
    rows = [
        finalize_row(
            {
                **empty_article_row(),
                "id": f"chembl:C{i}",
                "source": "chembl",
                "source_file": "F_a.db" if i % 2 else "F_b.db",
                "source_record_id": f"C{i}",
                "license": "CC BY-SA",
                "subset": "commercial",
            }
        )
        for i in range(n)
    ]
    info = write_rows(
        rows, tmp_path, source="chembl", source_file="F.db", prefer_parquet=prefer_parquet
    )
    return Path(info["paths"][0])


class _NoConn:
    """Any use of the connection fails the test: the guard must run first."""

    def __getattr__(self, name):
        raise AssertionError(f"connection touched: {name}")


@pytest.mark.parametrize("bad", [0, -1])
def test_load_source_file_rejects_batch_size_below_one(tmp_path, bad):
    shard = _shard(tmp_path, 3)
    with pytest.raises(ValueError, match="batch_size"):
        postgres_loader.load_source_file(
            _NoConn(), source="chembl", staging_path=shard, run_id="r", batch_size=bad
        )


def test_id_pass_streams_parquet_without_reading_whole_columns(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    def _no_read_table(*a, **k):
        raise AssertionError("pq.read_table loads whole columns into memory")

    monkeypatch.setattr(pq, "read_table", _no_read_table)
    shard = _shard(tmp_path, 7)
    batches = list(postgres_loader._iter_id_batches(shard, "parquet", 3))
    assert [len(ids) for ids, _ in batches] == [3, 3, 1]
    assert [i for ids, _ in batches for i in ids] == [f"chembl:C{i}" for i in range(7)]
    assert {sf for _, sfs in batches for sf in sfs} == {"F_a.db", "F_b.db"}


@pytest.mark.parametrize("reader", ["_iter_id_batches", "_iter_batches"])
def test_parquet_readers_close_the_file(tmp_path, monkeypatch, reader):
    import pyarrow.parquet as pq

    opened: list = []

    class _TrackedParquetFile(pq.ParquetFile):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.closed_by_reader = False
            opened.append(self)

        def close(self, *a, **k):
            self.closed_by_reader = True
            return super().close(*a, **k)

    monkeypatch.setattr(pq, "ParquetFile", _TrackedParquetFile)
    shard = _shard(tmp_path, 7)
    gen = getattr(postgres_loader, reader)(shard, "parquet", 3)
    next(gen)
    gen.close()  # abandoned mid-shard (e.g. the load raised)
    list(getattr(postgres_loader, reader)(shard, "parquet", 3))  # fully consumed
    assert len(opened) == 2
    assert all(pf.closed_by_reader for pf in opened)


class _FakeCopy:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        pass


class _FakeCur:
    """Records every statement; answers the planner-setting read with 'on'."""

    def __init__(self, log):
        self.log = log
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append(" ".join(sql.split()))

    def fetchone(self):
        return ("on", "on", "on", "on")

    def copy(self, sql):
        self.log.append(" ".join(sql.split()))
        return _FakeCopy()


class _FakeConn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _FakeCur(self.log)


def _load_log(tmp_path, monkeypatch):
    monkeypatch.setattr(postgres_loader.audit_trail, "record", lambda *a, **k: None)
    conn = _FakeConn()
    postgres_loader.load_source_file(
        conn, source="chembl", staging_path=_shard(tmp_path, 4), run_id="r"
    )
    return conn.log


def test_load_ids_table_is_sorted_and_distinct(tmp_path, monkeypatch):
    log = _load_log(tmp_path, monkeypatch)
    create = [s for s in log if s.startswith("CREATE TEMP TABLE pg_temp._load_ids ")]
    assert len(create) == 1
    assert "SELECT DISTINCT id FROM pg_temp._load_ids_raw ORDER BY id" in create[0]
    i_create = log.index(create[0])
    # the COPY fills the raw table, the sorted table is built from it, then ANALYZEd
    assert any(s.startswith("COPY pg_temp._load_ids_raw") for s in log[:i_create])
    assert "DROP TABLE pg_temp._load_ids_raw" in log[i_create + 1 :]
    assert "ANALYZE pg_temp._load_ids" in log[i_create + 1 :]


def test_planner_settings_wrap_only_the_two_id_probes(tmp_path, monkeypatch):
    log = _load_log(tmp_path, monkeypatch)
    off = ["SET LOCAL enable_hashjoin = off", "SET LOCAL enable_mergejoin = off"]
    off += ["SET LOCAL enable_seqscan = off", "SET LOCAL enable_hashagg = off"]
    on = [s.replace("= off", "= 'on'") for s in off]
    c2 = next(
        i for i, s in enumerate(log) if "article_id IN (SELECT id FROM pg_temp._load_ids)" in s
    )
    d0 = next(
        i for i, s in enumerate(log) if "id IN (SELECT id FROM pg_temp._load_ids) AND source" in s
    )
    # settings off right before (c2); (c2) and (d0) back to back; restored right after (d0)
    assert log[c2 - len(off) : c2] == off
    assert d0 == c2 + 1
    assert log[d0 + 1 : d0 + 1 + len(on)] == on
    # no other statement runs with the planner settings changed
    assert sum(s.startswith("SET LOCAL") for s in log) == 2 * len(off)
