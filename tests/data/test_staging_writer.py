import json
from pathlib import Path

from episteme.data.article_schema import empty_article_row, finalize_row
from episteme.data.staging_writer import write_rows


def _row(**kw):
    r = empty_article_row()
    r.update(kw)
    return r


def test_write_rows_parquet_one_shard_per_file(tmp_path):
    rows = [
        _row(id="pmc:PMC1", source="pmc", text="hello world", year=2024),
        _row(id="pmc:PMC2", source="pmc", text="another", year=None),
    ]
    res = write_rows(rows, tmp_path, source="pmc", source_file="PMC_batch_01.xml")
    assert res["format"] in ("parquet", "jsonl")
    assert res["n_rows"] == 2
    out = Path(res["paths"][0])
    assert out.parent == tmp_path / "staging" / "pmc"
    assert "PMC_batch_01" in out.name


def test_write_rows_jsonl_fallback_roundtrips(tmp_path, monkeypatch):
    import episteme.data.staging_writer as sw

    monkeypatch.setattr(sw, "_pyarrow_available", lambda: False)
    rows = [_row(id="pmc:PMC1", source="pmc", text="hi")]
    res = write_rows(rows, tmp_path, source="pmc", source_file="f.xml")
    assert res["format"] == "jsonl"
    line = json.loads(Path(res["paths"][0]).read_text().splitlines()[0])
    assert line["id"] == "pmc:PMC1"


def test_parquet_column_types_readback(tmp_path):
    """§8a: the staging Parquet shard pins real column types, not inferred nulls."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [
        finalize_row(
            {
                **empty_article_row(),
                "id": "pmc:PMC1",
                "source": "pmc",
                "title": "hello",
                "abstract": "world",
                "year": 2024,
                "authors": ["Ada L."],
                "mesh": ["Humans"],
                "publication_types": ["Journal Article"],
                "is_retracted": True,
                "is_manuscript": False,
                "is_historical_ocr": True,
            }
        ),
        finalize_row(
            {
                **empty_article_row(),
                "id": "pmc:PMC2",
                "source": "pmc",
                "title": "second",
                "abstract": "row",
                "year": 1999,
                "authors": ["Grace H.", "Alan T."],
                "mesh": ["Animals"],
                "publication_types": ["Review"],
                "is_retracted": False,
                "is_manuscript": True,
                "is_historical_ocr": False,
            }
        ),
    ]
    res = write_rows(rows, tmp_path, source="pmc", source_file="T.json")
    assert res["format"] == "parquet"
    schema = pq.read_table(res["paths"][0]).schema

    assert pa.types.is_integer(schema.field("year").type)
    for col in ("authors", "mesh", "publication_types"):
        assert schema.field(col).type == pa.list_(pa.string())
    for col in ("is_retracted", "is_manuscript", "is_historical_ocr"):
        assert schema.field(col).type == pa.bool_()


def _mixed_rows():
    return [
        _row(id="pmc:1", source="pmc", title="a", year=2024, authors=["A"], is_retracted=True),
        _row(id="pmc:2", source="pmc", title="b", year=None, authors=None, mesh=None),
        _row(id="pmc:3", source="pmc", title="c", year=1999, mesh=["M"], publication_types=["R"]),
        _row(id="pmc:4", source="pmc", title="d", year=2001, authors=None, mesh=None),
        _row(id="pmc:5", source="pmc", title="e", year=None, publication_types=None),
    ]


def test_multi_batch_equals_single_batch_and_shard(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from episteme.data.staging_writer import write_parquet_shard

    rows = _mixed_rows()
    multi = write_rows(rows, tmp_path / "m", source="pmc", source_file="f.xml", batch_size=2)
    single = write_rows(rows, tmp_path / "s", source="pmc", source_file="f.xml", batch_size=1000)
    shard = write_parquet_shard(rows, tmp_path / "w", source="pmc", source_file="f.xml")
    assert multi["n_rows"] == 5 and multi["format"] == "parquet"
    tm = pq.read_table(multi["paths"][0])
    assert tm.equals(pq.read_table(single["paths"][0]))
    assert tm.equals(pq.read_table(shard[0]))
    assert tm.schema.field("year").type == pa.int32()
    assert tm.schema.field("authors").type == pa.list_(pa.string())
    assert pq.ParquetFile(multi["paths"][0]).num_row_groups == 3


def test_all_none_list_batch_keeps_schema(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [
        _row(id="pmc:1", authors=["A"], year=2000),
        _row(id="pmc:2", authors=None, mesh=None, year=None),
    ]
    res = write_rows(rows, tmp_path, source="pmc", source_file="f.xml", batch_size=1)
    t = pq.read_table(res["paths"][0])
    assert t.schema.field("authors").type == pa.list_(pa.string())
    assert t.schema.field("year").type == pa.int32()
    assert t.schema.field("is_retracted").type == pa.bool_()


def test_generator_input_works(tmp_path):
    res = write_rows(
        (r for r in _mixed_rows()), tmp_path, source="pmc", source_file="g.xml", batch_size=2
    )
    assert res["n_rows"] == 5
    assert len(res["paths"]) == 1


def test_generator_failure_leaves_no_file(tmp_path):
    import pytest

    def gen():
        for i, r in enumerate(_mixed_rows()):
            if i == 3:
                raise ValueError("boom")
            yield r

    with pytest.raises(ValueError, match="boom"):
        write_rows(gen(), tmp_path, source="pmc", source_file="x.xml", batch_size=2)
    base = tmp_path / "staging" / "pmc"
    assert not list(base.glob("*.parquet"))
    assert not list(base.glob("*.tmp"))


def test_empty_iterator_writes_nothing(tmp_path):
    res = write_rows(iter([]), tmp_path, source="pmc", source_file="e.xml")
    assert res == {"format": "parquet", "paths": [], "n_rows": 0}
    base = tmp_path / "staging" / "pmc"
    assert not base.exists() or not list(base.iterdir())


def test_jsonl_fallback_streams_and_cleans_up(tmp_path, monkeypatch):
    import pytest

    import episteme.data.staging_writer as sw

    monkeypatch.setattr(sw, "_pyarrow_available", lambda: False)
    res = write_rows((r for r in _mixed_rows()), tmp_path, source="pmc", source_file="j.xml")
    assert res["format"] == "jsonl" and res["n_rows"] == 5
    assert len(Path(res["paths"][0]).read_text().splitlines()) == 5

    def gen():
        yield _row(id="pmc:1")
        raise ValueError("boom")

    with pytest.raises(ValueError):
        write_rows(gen(), tmp_path, source="pmc", source_file="k.xml")
    base = tmp_path / "staging" / "pmc"
    assert not (base / "k.jsonl").exists()
    assert not list(base.glob("*.tmp"))


def test_write_rows_rejects_batch_size_below_one(tmp_path):
    import pytest

    for bad in (0, -1):
        with pytest.raises(ValueError, match="batch_size"):
            write_rows(_mixed_rows(), tmp_path, source="pmc", source_file="b.xml", batch_size=bad)
    assert not (tmp_path / "staging").exists()


def _text_rows(n, chars):
    return [
        finalize_row(_row(id=f"pmc:{i}", source="pmc", source_file="f.xml", text="x" * chars))
        for i in range(n)
    ]


def test_a_batch_also_ends_at_the_byte_cap(tmp_path, monkeypatch):
    # 2026-10-26: europepmc full-text rows (~58 KB each) made one 50,000-row
    # batch ~3 GB; the byte cap bounds a batch whatever the row size.
    import pyarrow.parquet as pq

    from episteme.data import staging_writer

    rows = _text_rows(10, 400)
    one = staging_writer._row_chars(rows[0])  # every row has the same size
    assert all(staging_writer._row_chars(r) == one for r in rows)
    # 2.5 rows of string data: a batch ends at its 3rd row; the row cap (1,000) never binds.
    monkeypatch.setattr(staging_writer, "WRITE_BATCH_BYTES", int(2.5 * one))
    capped = write_rows(rows, tmp_path / "c", source="pmc", source_file="f.xml", batch_size=1000)
    pf = pq.ParquetFile(capped["paths"][0])
    sizes = [pf.metadata.row_group(i).num_rows for i in range(pf.num_row_groups)]
    assert sizes == [3, 3, 3, 1]
    monkeypatch.setattr(staging_writer, "WRITE_BATCH_BYTES", 10**12)
    plain = write_rows(rows, tmp_path / "p", source="pmc", source_file="f.xml", batch_size=1000)
    assert pq.read_table(capped["paths"][0]).equals(pq.read_table(plain["paths"][0]))


def test_a_row_over_the_byte_cap_is_a_batch_of_its_own(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    from episteme.data import staging_writer

    monkeypatch.setattr(staging_writer, "WRITE_BATCH_BYTES", 100)
    res = write_rows(_text_rows(3, 500), tmp_path, source="pmc", source_file="f.xml", batch_size=10)
    pf = pq.ParquetFile(res["paths"][0])
    assert [pf.metadata.row_group(i).num_rows for i in range(pf.num_row_groups)] == [1, 1, 1]
    assert res["n_rows"] == 3
