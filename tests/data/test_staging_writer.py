import json
from pathlib import Path

from episteme.data.staging_writer import write_rows
from episteme.data.article_schema import empty_article_row


def _row(**kw):
    r = empty_article_row()
    r.update(kw)
    return r


def test_write_rows_parquet_one_shard_per_file(tmp_path):
    rows = [_row(id="pmc:PMC1", source="pmc", text="hello world", year=2024),
            _row(id="pmc:PMC2", source="pmc", text="another", year=None)]
    res = write_rows(rows, tmp_path, source="pmc", source_file="PMC_batch_01.xml")
    assert res["format"] in ("parquet", "jsonl")
    assert res["n_rows"] == 2
    out = Path(res["paths"][0])
    assert out.parent == tmp_path / "staging" / "pmc"
    assert "PMC_batch_01" in out.name


def test_write_rows_jsonl_fallback_roundtrips(tmp_path, monkeypatch):
    import episteme.data.staging_writer as sw
    monkeypatch.setattr(sw, "write_parquet_shard", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pyarrow")))
    rows = [_row(id="pmc:PMC1", source="pmc", text="hi")]
    res = write_rows(rows, tmp_path, source="pmc", source_file="f.xml")
    assert res["format"] == "jsonl"
    line = json.loads(Path(res["paths"][0]).read_text().splitlines()[0])
    assert line["id"] == "pmc:PMC1"
