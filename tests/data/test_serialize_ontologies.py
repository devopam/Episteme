"""SP4 Task 11 -- ontologies structured serializer.

Fixture: tests/fixtures/sp4/ontologies/sample.obo -- a real slice of the
Human Phenotype Ontology, fetched directly at implementation time
(2026-09-17, ``http://purl.obolibrary.org/obo/hp.obo``, real header + 3 real
``[Term]`` stanzas, ``HP:0000001``/``HP:0000002``/``HP:0000003``, verbatim
names/definitions from the real 2026-09-01 HPO release -- not fabricated).
HPO was picked over GO/MONDO for both the fixture and the real end-to-end
run: all three ship as OBO (``pronto.Ontology`` handles all identically) but
HPO's real ``hp.obo`` is the smallest of the three (10.8MB vs GO's 36.7MB and
MONDO's 53.1MB, confirmed via a real HEAD request at implementation time),
the most practical to fetch in full within budget.

Real per-term shape (confirmed against the real 2026-09-01 file, and against
pronto 2.7.3 / fastobo 0.14.1, the versions actually installed in this
repo's venv): ``pronto.Ontology(path)`` exposes ``onto.terms()`` (unordered
-- sorted by ``term.id`` here for determinism); each ``Term`` has ``.id``
(the CURIE, e.g. ``HP:0000001`` -- pronto does NOT further-namespace it,
matches the brief's ``id=f"ontologies:{term.id}"`` framing exactly),
``.name`` (str), and ``.definition`` (a ``pronto.definition.Definition``
object with a plain ``str()`` giving the definition text, or ``None`` when
the term carries no ``def:`` line -- confirmed real, structurally optional,
e.g. this fixture's own ``HP:0000001`` "All" root term).

MIN_OK_TEXT_LEN (200 chars) -- checked against ALL THREE fixture rows, not
just a well-defined/sparse pair, because the real finding needed a third
data point: name+definition together do not reliably clear 200 chars for
real HPO terms.
  * HP:0000003 "Multicystic kidney dysplasia": real name+definition text is
    290 chars -> comfortably clears 200 -> ``extract_status="ok"``.
  * HP:0000002 "Abnormality of body height": real name+definition text is
    only 135 chars (`the def alone is 107 chars -- a real, common HPO
    shape: many definitions are short one-sentence glosses, not
    paragraphs`) -> under 200 -> ``extract_status="partial"``, despite
    carrying a genuine, non-empty definition. This is the finding this
    module's docstring flags explicitly: unlike MeSH/Reactome (which pad
    their templates with a trailing boilerplate sentence to help clear the
    gate), this task's brief pins ``text=f"{name}: {definition}"`` literally
    with no boilerplate padding -- so a real, honest fraction of
    name+definition rows still land "partial". Not a bug.
  * HP:0000001 "All" (no definition at all, name-only fallback): 3 chars ->
    ``extract_status="partial"``. Never "empty": every ``[Term]`` stanza
    always carries a non-empty ``name:`` line in practice, and this
    module's own defensive fallback (``term.name or native_id``) guarantees
    a non-empty title even in the theoretical case it doesn't.

Licence: HPO's real header declares ``property_value: terms:license
https://hpo.jax.org/app/license`` -- a bare project URL, no CC/SPDX token
anywhere in it. Run through the ordinary
``article_schema.normalize_license()``/``subset_from_license()`` machinery
(NOT a hardcode): confirmed it resolves to ``license="unknown"`` ->
``subset="open_metadata"`` -- no existing arm matches a bare non-CC URL.
FLAGGED, not forced (spec Sec 8 item 4) -- see module docstring for the
full four-way (GO/HPO/MONDO/UCUM) licence finding; ``article_schema.py`` is
NOT touched by this task's diff.

All `not pg` -- no DB.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.ontologies.serialize_ontologies import (
    discover_ontology_files,
    iter_rows_from_file,
    main,
    serialize_ontologies,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp4" / "ontologies"


def _read_shard(staging_dir: Path) -> pl.DataFrame:
    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_ontologies_serialize_rows(tmp_path):
    res = serialize_ontologies(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] == 3

    df = _read_shard(tmp_path / "staging" / "ontologies")
    rows = df.to_dicts()

    assert {row["source"] for row in rows} == {"ontologies"}
    assert all(row["id"].startswith("ontologies:") for row in rows)
    assert {row["id"] for row in rows} == {
        "ontologies:HP:0000001",
        "ontologies:HP:0000002",
        "ontologies:HP:0000003",
    }
    assert {row["source_record_id"] for row in rows} == {
        "HP:0000001",
        "HP:0000002",
        "HP:0000003",
    }

    # HPO's real declared license is a bare project URL with no CC/SPDX
    # token -- normalize_license's existing arms genuinely don't match it.
    # FLAGGED, not forced.
    assert all(row["license"] == "unknown" for row in rows)
    assert all(row["subset"] == "open_metadata" for row in rows)
    assert all(row["license_raw"] == "https://hpo.jax.org/app/license" for row in rows)
    assert all(row["license_url"] is None for row in rows)

    assert all(row["container_id"] is None for row in rows)
    assert all(row["book_meta"] is None for row in rows)
    assert all(row["journal"] is None for row in rows)
    assert all(row["year"] is None for row in rows)
    assert all(row["authors"] is None for row in rows)
    assert all(row["pmid"] is None and row["pmcid"] is None and row["doi"] is None for row in rows)
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)

    by_id = {row["id"]: row for row in rows}

    ok_row = by_id["ontologies:HP:0000003"]
    assert ok_row["title"] == "Multicystic kidney dysplasia"
    assert ok_row["text"] == (
        "Multicystic kidney dysplasia: Multicystic dysplasia of the kidney is "
        "characterized by multiple cysts of varying size in the kidney and the "
        "absence of a normal pelvicaliceal system. The condition is associated "
        "with ureteral or ureteropelvic atresia, and the affected kidney is "
        "nonfunctional."
    )
    assert len(ok_row["text"]) == 290
    assert ok_row["extract_status"] == "ok"

    partial_with_def = by_id["ontologies:HP:0000002"]
    assert partial_with_def["title"] == "Abnormality of body height"
    assert partial_with_def["text"] == (
        "Abnormality of body height: Deviation from the norm of height with "
        "respect to that which is expected according to age and gender norms."
    )
    assert len(partial_with_def["text"]) == 135
    # Real, honest finding: a genuine name+definition pair that still lands
    # under MIN_OK_TEXT_LEN -- not a bug, see module docstring.
    assert partial_with_def["extract_status"] == "partial"

    name_only = by_id["ontologies:HP:0000001"]
    assert name_only["title"] == "All"
    assert name_only["text"] == "All"
    assert name_only["extract_status"] == "partial"


def test_ontologies_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_ontologies_serialize_is_idempotent_without_force(tmp_path):
    res1 = serialize_ontologies(FX, tmp_path)
    assert res1["ok"] == 1
    # Second run without --force must skip (success marker present).
    res2 = serialize_ontologies(FX, tmp_path)
    assert res2["inputs"] == 1
    assert res2["ok"] == 0  # skipped, not re-ok'd
    assert res2["failed"] == 0

    res3 = serialize_ontologies(FX, tmp_path, force=True)
    assert res3["ok"] == 1


def test_discovery_recurses_into_per_vocabulary_subdirectories():
    """Real download_ontologies.sh layout nests each vocabulary in its own
    subdirectory (01_raw/ontologies/go/go.obo, .../hpo/hp.obo,
    .../mondo/mondo.obo) -- unlike mesh's/reactome's flat raw dirs. A unit
    test against this module's own flat fixture dir alone would not catch a
    discovery routine that only globs top-level files; this test proves
    recursion into subdirectories independently of the flat-fixture tests
    above."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        raw_dir = Path(td)
        (raw_dir / "go").mkdir()
        (raw_dir / "hpo").mkdir()
        shutil.copy(FX / "sample.obo", raw_dir / "go" / "go.obo")
        shutil.copy(FX / "sample.obo", raw_dir / "hpo" / "hp.obo")

        files = discover_ontology_files(raw_dir)
        assert {f.name for f in files} == {"go.obo", "hp.obo"}
        assert len(files) == 2


def test_discovery_prefers_obo_over_owl_in_same_directory():
    """download_ontologies.sh fetches BOTH go.obo and go.owl (same
    vocabulary, two serializations, identical term CURIEs) into the SAME
    subdirectory. Processing both would manufacture a cross-source_file id
    collision (two rows sharing one primary key) -- exactly the class of bug
    the SP4 plan's own loader-hardening goal targets. Discovery must pick
    exactly one file per vocabulary directory, preferring .obo (pronto's
    primary/fastest path; smaller download)."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        raw_dir = Path(td)
        vocab_dir = raw_dir / "go"
        vocab_dir.mkdir()
        shutil.copy(FX / "sample.obo", vocab_dir / "go.obo")
        # Dummy .owl sibling -- content is irrelevant, it must never be
        # selected/parsed when a .obo sibling is present in the same dir.
        (vocab_dir / "go.owl").write_text("<rdf:RDF>not a real owl file</rdf:RDF>")

        files = discover_ontology_files(raw_dir)
        assert len(files) == 1
        assert files[0].name == "go.obo"


def test_discovery_falls_back_to_owl_when_no_obo_present():
    """If only the .owl file exists in a vocabulary directory (e.g. a
    partial/--max-files-capped real download), discovery must still surface
    it rather than silently dropping that vocabulary -- pronto is not
    exercised against a real .owl file by this test suite (out of the real
    download's practical fetch order this task budgeted for), only the
    discovery/selection logic."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        raw_dir = Path(td)
        vocab_dir = raw_dir / "go"
        vocab_dir.mkdir()
        (vocab_dir / "go.owl").write_text("<rdf:RDF>not a real owl file</rdf:RDF>")

        files = discover_ontology_files(raw_dir)
        assert len(files) == 1
        assert files[0].name == "go.owl"


def test_ontologies_report_respects_max_files(tmp_path, capsys):
    raw_dir = tmp_path / "raw"
    (raw_dir / "go").mkdir(parents=True)
    (raw_dir / "hpo").mkdir(parents=True)
    shutil.copy(FX / "sample.obo", raw_dir / "go" / "go.obo")
    shutil.copy(FX / "sample.obo", raw_dir / "hpo" / "hp.obo")

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

    rc = main(["--raw-dir", str(raw_dir), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    uncapped_out = capsys.readouterr().out

    assert "files=1  rows=3" in capped_out
    assert "files=2  rows=6" in uncapped_out


def test_ontologies_no_license_property_falls_back_to_unknown(tmp_path):
    """Defensive: an ontology file with no ``license`` property_value at all
    (not observed in the real GO/HPO/MONDO headers, all three declare one --
    see module docstring -- but not guaranteed by the OBO format) must not
    raise; normalize_license(None) already resolves this to
    unknown/open_metadata, same as every row's baseline."""
    obo_text = (
        "format-version: 1.2\n"
        "ontology: test\n"
        "\n"
        "[Term]\n"
        "id: TST:0000001\n"
        "name: term without any license header\n"
    )
    raw_dir = tmp_path / "raw" / "test"
    raw_dir.mkdir(parents=True)
    (raw_dir / "test.obo").write_text(obo_text)

    rows = list(iter_rows_from_file(raw_dir / "test.obo"))
    assert len(rows) == 1
    assert rows[0]["license"] == "unknown"
    assert rows[0]["license_raw"] is None
    assert rows[0]["subset"] == "open_metadata"
