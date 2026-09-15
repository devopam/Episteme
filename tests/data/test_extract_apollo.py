"""SP2 Task 6 — apollo extractor, reconciled to the extract_pubmed shape.

Fixture: tests/fixtures/sp2/apollo/apollo_sample.jsonl — 3 ApolloCorpus docs:
(a) English medical prose >= 300 chars, lang tag "en"; (b) Spanish medical prose
>= 300 chars, language tag "es" (proves multilingual survives with
APOLLO_MEDICAL_GATE=False); (c) short boilerplate < 100 chars -> extract_status
"dropped" via the length gate.

All `not pg` — no DB.
"""

from __future__ import annotations

from pathlib import Path

from episteme.data import article_schema
from episteme.data.apollo.extract_apollo import extract_apollo, main

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "apollo"


def _read_shard(staging_dir: Path):
    import polars as pl

    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_apollo_extract_rows(tmp_path):
    res = extract_apollo(FX, tmp_path)
    assert res["inputs"] >= 1
    assert res["failed"] == 0
    assert res["rows"] >= 1

    df = _read_shard(tmp_path / "staging" / "apollo")
    rows = df.to_dicts()
    assert {r["source"] for r in rows} == {"apollo"}
    assert all(r["extract_status"] in article_schema.EXTRACT_STATUSES for r in rows)
    # ApolloCorpus is not PubMed-indexed and carries no container.
    assert all(r["pmid"] is None for r in rows)
    assert all(r["pmcid"] is None for r in rows)
    assert all(r["container_id"] is None for r in rows)
    assert all(r["book_meta"] is None for r in rows)
    assert all(r["mesh"] is None for r in rows)

    # the English medical doc: language tag surfaced, text == body verbatim
    # (finalize_row did NOT rebuild text as title + "\n\n" + body)
    en = next(r for r in rows if "acetylcholinesterase" in (r["text"] or "").lower())
    assert en["language"] == "en"
    assert en["extract_status"] == "ok"
    assert en["text"] == en["body_text"]
    assert "\n\n" not in (en["text"] or "")  # no build_text title/body join
    assert en["title"] and en["title"] in en["text"]

    # the Spanish medical doc survives with the default gate off
    es = next(r for r in rows if r["language"] == "es")
    assert es["extract_status"] == "ok"
    assert "hipertension" in (es["text"] or "").lower()


def test_apollo_drops_boilerplate(tmp_path):
    extract_apollo(FX, tmp_path)
    df = _read_shard(tmp_path / "staging" / "apollo")
    rows = df.to_dicts()
    dropped = [r for r in rows if r["extract_status"] == "dropped"]
    assert len(dropped) == 1
    d = dropped[0]
    assert (d["extract_notes"] or "").startswith("drop_heuristic")
    assert "terms of use" in (d["text"] or "").lower()


def test_apollo_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out  # a field-shape table was printed
    # --report writes NO shards, NO markers, NO run manifest
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()
