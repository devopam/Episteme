"""SP4 Task 10 -- mesh structured serializer (also proves graph_builder's
Task 4 mesh_hierarchy derivation end-to-end against real data -- see
task-10-report.md for that proof; this file covers only the ``pg``-free
serializer unit tests).

Fixture: tests/fixtures/sp4/mesh/sample.xml -- reuses Task 4's exact merged
synthetic-fixture SHAPE and content (test_graph_builder.py::
test_build_populates_mesh_hierarchy -- same D003924/D003920 UIs, names and
tree numbers, unmodified), extended with a real
ConceptList/Concept[@PreferredConceptYN='Y']/ScopeNote on D003924 only
(D003920 stays bare) so this file's own test exercises both sides of
MIN_OK_TEXT_LEN from one file. The added ScopeNote text is real NLM prose,
copied verbatim from the real 2025 MeSH descriptor release's actual
"Diabetes Mellitus, Type 2" concept (~400 chars, long enough to clear
MIN_OK_TEXT_LEN once combined with the name + closing clause) -- attached
here to the fixture's D003924 record (named "Diabetes Mellitus" per Task 4's
fixture), a deliberate, flagged mismatch: the point of this fixture is to
exercise the ok/partial split with genuine real prose, not to reproduce the
real file's true name<->ScopeNote pairing (which is already broken by Task
4's own UI/name/tree-number swap -- see serialize_mesh.py's module docstring
for the full confirmed-against-the-real-file swap, preserved unmodified).

All `not pg` -- no DB.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.mesh.serialize_mesh import (
    discover_mesh_files,
    iter_rows_from_file,
    main,
    serialize_mesh,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp4" / "mesh"


def _read_shard(staging_dir: Path) -> pl.DataFrame:
    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_mesh_serialize_rows(tmp_path):
    res = serialize_mesh(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] >= 1
    assert res["rows"] == 2

    shards = list((tmp_path / "staging" / "mesh").glob("*.*"))
    assert shards
    df = pl.read_parquet(shards[0]) if shards[0].suffix == ".parquet" else pl.read_ndjson(shards[0])
    rows = df.to_dicts()

    r = next(x for x in rows if x["source_record_id"] == "D003924")
    assert r["id"] == "mesh:D003924"
    assert "Diabetes Mellitus" in (r["text"] or r["title"] or "")
    assert r["container_id"] is None

    assert {row["source"] for row in rows} == {"mesh"}
    assert all(row["id"].startswith("mesh:") for row in rows)
    assert {row["id"] for row in rows} == {"mesh:D003924", "mesh:D003920"}
    assert all(row["book_meta"] is None for row in rows)
    assert all(row["journal"] is None and row["year"] is None for row in rows)
    assert all(row["authors"] is None for row in rows)
    assert all(row["pmid"] is None and row["pmcid"] is None and row["doi"] is None for row in rows)
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)

    # Source-anchored governance override (SP4.1 Task 11): public_domain ->
    # commercial; license_raw keeps the real NLM terms text.
    assert all(row["license"] == "public_domain" for row in rows)
    assert all(row["subset"] == "commercial" for row in rows)
    assert all("National Library of Medicine" in row["license_raw"] for row in rows)

    by_id = {row["id"]: row for row in rows}

    d003924 = by_id["mesh:D003924"]
    assert d003924["title"] == "Diabetes Mellitus"
    assert "INSULIN RESISTANCE" in d003924["text"]
    # Real, well-annotated (long) ScopeNote clears MIN_OK_TEXT_LEN.
    assert d003924["extract_status"] == "ok"

    d003920 = by_id["mesh:D003920"]
    assert d003920["title"] == "Diabetes Mellitus, Type 2"
    assert d003920["text"] == (
        "Diabetes Mellitus, Type 2. "
        "As defined in the NLM Medical Subject Headings (MeSH) thesaurus."
    )
    # No ScopeNote at all -> short bare-name sentence, honest "partial", not
    # a bug -- mirrors every prior SP4 structured source's near-boundary
    # finding for sparse records. Never "empty": DescriptorName is always
    # non-empty.
    assert d003920["extract_status"] == "partial"


def test_mesh_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_mesh_serialize_is_idempotent_without_force(tmp_path):
    res1 = serialize_mesh(FX, tmp_path)
    assert res1["ok"] == 1
    # Second run without --force must skip (success marker present).
    res2 = serialize_mesh(FX, tmp_path)
    assert res2["inputs"] == 1
    assert res2["ok"] == 0  # skipped, not re-ok'd
    assert res2["failed"] == 0

    res3 = serialize_mesh(FX, tmp_path, force=True)
    assert res3["ok"] == 1


def test_mesh_gzip_input_is_supported(tmp_path):
    """A real download could plausibly land as ``desc<year>.gz`` (confirmed
    the REAL shape at https://nlmpubs.nlm.nih.gov/projects/mesh/2025/xmlmesh/
    -- desc2025.gz is the compressed sibling of desc2025.xml, 16,840,289
    bytes vs. 314,015,481 uncompressed) -- confirm gzip-compressed input
    parses identically to the bare fixture."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    src = FX / "sample.xml"
    dst = raw_dir / "desc2025.gz"
    with src.open("rb") as fsrc, gzip.open(dst, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst)

    processed_dir = tmp_path / "processed"
    res = serialize_mesh(raw_dir, processed_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["rows"] == 2

    df = _read_shard(processed_dir / "staging" / "mesh")
    rows = df.to_dicts()
    assert {row["id"] for row in rows} == {"mesh:D003924", "mesh:D003920"}


def test_mesh_discover_ignores_non_descriptor_siblings(tmp_path):
    """A real full mesh download can land desc/qual/supp/pa XML files side
    by side in the same directory (confirmed via the real 2025 xmlmesh/
    listing). Only the DescriptorRecordSet file must be discovered -- the
    generic "*.xml" fallback pattern alone would match all four; the
    root-tag peek (_is_descriptor_file) is what actually filters them out."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(FX / "sample.xml", raw_dir / "desc2025.xml")
    (raw_dir / "qual2025.xml").write_text(
        "<QualifierRecordSet><QualifierRecord><QualifierUI>Q000008</QualifierUI>"
        "<QualifierName><String>administration &amp; dosage</String></QualifierName>"
        "</QualifierRecord></QualifierRecordSet>",
        encoding="utf-8",
    )
    (raw_dir / "pa2025.xml").write_text(
        "<PharmacologicalActionSet><PharmacologicalAction>"
        "<DescriptorReferredTo><DescriptorUI>D000001</DescriptorUI></DescriptorReferredTo>"
        "</PharmacologicalAction></PharmacologicalActionSet>",
        encoding="utf-8",
    )

    files = discover_mesh_files(raw_dir)
    assert [f.name for f in files] == ["desc2025.xml"]


def test_mesh_scope_note_prefers_preferred_concept():
    """Directly exercises _preferred_scope_note via iter_rows_from_file:
    D003924's row must carry the ScopeNote text, D003920's must not (no
    ConceptList at all in the fixture for that record)."""
    rows = list(iter_rows_from_file(FX / "sample.xml"))
    by_id = {r["source_record_id"]: r for r in rows}
    assert "HYPERGLYCEMIA" in by_id["D003924"]["text"]
    assert by_id["D003920"]["text"].startswith("Diabetes Mellitus, Type 2.")
    assert "ScopeNote" not in by_id["D003920"]["text"]


def test_mesh_report_respects_max_files(tmp_path, capsys):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(FX / "sample.xml", raw_dir / "desc2025.xml")
    shutil.copy(FX / "sample.xml", raw_dir / "desc2026.xml")

    rc = main(
        [
            "--raw-dir",
            str(raw_dir),
            "--processed-dir",
            str(tmp_path),
            "--report",
            "--max-files",
            "1",
        ]
    )
    assert rc == 0
    capped_out = capsys.readouterr().out

    rc = main(
        [
            "--raw-dir",
            str(raw_dir),
            "--processed-dir",
            str(tmp_path),
            "--report",
            "--max-files",
            "2",
        ]
    )
    assert rc == 0
    wider_out = capsys.readouterr().out

    assert "files=1" in capped_out
    assert "files=2" in wider_out
