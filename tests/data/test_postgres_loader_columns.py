"""Column-wise COPY tuples equal the old dict path's; timing fields present. No DB."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from episteme.data import postgres_loader
from episteme.data.article_schema import empty_article_row, finalize_row
from episteme.data.staging_writer import write_rows


def _rows() -> list[dict]:
    out = []
    for i in range(5):
        out.append(
            finalize_row(
                {
                    **empty_article_row(),
                    "id": f"pmid:{i}",
                    "source": "pubmed",
                    "source_file": "baseline__f.xml.gz",
                    "source_record_id": str(i),
                    "pmid": str(i),
                    "title": f"Title {i}",
                    "abstract": None if i % 2 else f"Abstract {i}",
                    "authors": [f"A {i}", "B"] if i % 2 else None,
                    "mesh": ["Humans", f"M{i}"],
                    "year": None if i == 3 else 2000 + i,
                    "license": "unknown",
                    "subset": "open_metadata",
                }
            )
        )
    return out


def _shard(tmp_path: Path, *, parquet: bool) -> Path:
    info = write_rows(
        _rows(), tmp_path, source="pubmed", source_file="baseline__f.xml.gz", prefer_parquet=parquet
    )
    return Path(info["paths"][0])


def _dicts(shard: Path) -> list[dict]:
    if shard.suffix == ".parquet":
        return pq.read_table(shard).to_pylist()
    return [json.loads(x) for x in shard.read_text(encoding="utf-8").splitlines() if x.strip()]


def _legacy_tuples(rows: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """The pre-change dict path, kept verbatim as the reference."""
    hot = [
        tuple(postgres_loader._hot_value(r, c) for c in postgres_loader.HOT_COLUMNS) for r in rows
    ]
    body = [
        (
            r["id"],
            r.get("source"),
            postgres_loader._coerce_year(r.get("year")),
            r.get("title"),
            r.get("abstract"),
            r.get("body_text"),
            r.get("text"),
        )
        for r in rows
    ]
    return hot, body


class _Copy:
    def __init__(self, sink: list):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        self.sink.append(tuple(row))


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.rowcount = 0

    def fetchone(self):
        return ("on", "on", "on", "on", True)  # the planner settings read

    def copy(self, sql):
        target = sql.split("(")[0].replace("COPY", "").strip()
        return _Copy(self.conn.copied.setdefault(target, []))


class _Conn:
    def __init__(self):
        self.copied: dict[str, list] = {}

    def cursor(self):
        return _Cursor(self)


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(postgres_loader.audit_trail, "record", lambda *a, **k: "h")


@pytest.mark.parametrize("parquet", [True, False], ids=["parquet", "jsonl"])
def test_column_path_sends_the_same_tuples_as_the_dict_path(tmp_path, parquet):
    shard = _shard(tmp_path, parquet=parquet)
    conn = _Conn()
    result = postgres_loader.load_source_file(
        conn, source="pubmed", staging_path=shard, run_id="r", batch_size=2
    )
    hot, body = _legacy_tuples(_dicts(shard))
    assert conn.copied["episteme.articles"] == hot
    assert conn.copied["episteme.article_body"] == body
    assert result["rows"] == 5


def test_missing_optional_column_reads_as_none(tmp_path):
    shard = _shard(tmp_path, parquet=True)
    table = pq.read_table(shard).drop_columns(["book_meta", "container_id"])
    slim = tmp_path / "slim.parquet"
    pq.write_table(table, slim)
    conn = _Conn()
    postgres_loader.load_source_file(conn, source="pubmed", staging_path=slim, run_id="r")
    hot, body = _legacy_tuples(table.to_pylist())  # dicts lack the keys -> r.get() is None
    assert conn.copied["episteme.articles"] == hot
    assert conn.copied["episteme.article_body"] == body


def test_result_carries_timing_fields(tmp_path):
    shard = _shard(tmp_path, parquet=True)
    result = postgres_loader.load_source_file(
        _Conn(), source="pubmed", staging_path=shard, run_id="r"
    )
    for key in ("read_sec", "copy_sec", "rows_per_sec"):
        assert isinstance(result[key], float), key
        assert result[key] >= 0.0, key


def test_batch_tuples_cap_oversized_list_elements_from_old_shards():
    # A shard staged before the cap (schema < 1.5) can carry a 5,658-byte
    # CollectiveName; the COPY path caps it so the GIN index accepts the row.
    from episteme.data.article_schema import GIN_LIST_COLUMNS, MAX_LIST_ELEMENT_BYTES

    row = {c: None for c in postgres_loader.article_schema.ARTICLE_COLUMNS}
    row.update(
        id="pmid:9",
        source="pubmed",
        year=2020,
        authors=["Jane Doe", "Team: " + "X Y, " * 2000],
        mesh=None,
        publication_types=["Journal Article"],
    )
    cols = {c: [row[c]] for c in row}
    hot, _ = postgres_loader._batch_tuples(cols)
    for c in GIN_LIST_COLUMNS:
        v = hot[0][postgres_loader.HOT_COLUMNS.index(c)]
        assert v is None or all(len(e.encode("utf-8")) <= MAX_LIST_ELEMENT_BYTES for e in v)
    authors = hot[0][postgres_loader.HOT_COLUMNS.index("authors")]
    assert authors[0] == "Jane Doe"
    assert authors[1].endswith("…")
