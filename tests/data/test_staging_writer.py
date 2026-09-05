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

    monkeypatch.setattr(
        sw, "write_parquet_shard", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pyarrow"))
    )
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
