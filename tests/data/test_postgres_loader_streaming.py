"""Non-pg checks of postgres_loader's streaming readers."""

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
