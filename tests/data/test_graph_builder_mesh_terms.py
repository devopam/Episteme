"""MeSH term parse (non-pg) and the mesh graph phase filling mesh_term (pg)."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from episteme.data.graph_builder import _parse_mesh_terms, mesh_release_of
from episteme.reference.term_norm import term_norm

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0017 = REPO_ROOT / "src/episteme/data/db/migrations/0017_api_no_timeouts.sql"
MIGRATION_0018 = REPO_ROOT / "src/episteme/data/db/migrations/0018_question_terms.sql"


def _term(ui: str, text: str, permuted: str = "N") -> str:
    return (
        f'<Term ConceptPreferredTermYN="N" IsPermutedTermYN="{permuted}" '
        f'RecordPreferredTermYN="N"><TermUI>{ui}</TermUI><String>{text}</String></Term>'
    )


_XML = (
    "<DescriptorRecordSet>"
    "<DescriptorRecord><DescriptorUI>D003924</DescriptorUI>"
    "<DescriptorName><String>Diabetes Mellitus</String></DescriptorName>"
    "<ConceptList>"
    '<Concept PreferredConceptYN="Y"><ConceptUI>M0006000</ConceptUI>'
    "<ConceptName><String>Diabetes Mellitus</String></ConceptName>"
    "<ConceptRelationList>"
    '<ConceptRelation RelationName="NRW"><Concept1UI>M0006000</Concept1UI>'
    "<Concept2UI>M0006001</Concept2UI></ConceptRelation>"
    "</ConceptRelationList>"
    "<TermList>"
    '<Term ConceptPreferredTermYN="Y" IsPermutedTermYN="N" RecordPreferredTermYN="Y">'
    "<TermUI>T000001</TermUI><String>Diabetes Mellitus</String></Term>"
    f"{_term('T000002', 'Diabetes')}"
    f"{_term('T000003', 'Mellitus, Diabetes', permuted='Y')}"
    f"{_term('T000004', 'Diabetes')}"
    "</TermList></Concept>"
    '<Concept PreferredConceptYN="N"><ConceptUI>M0006001</ConceptUI>'
    "<ConceptName><String>Sugar Diabetes</String></ConceptName>"
    f"<TermList>{_term('T000005', 'Sugar Diabetes')}</TermList></Concept>"
    "</ConceptList></DescriptorRecord>"
    "<DescriptorRecord><DescriptorUI>D003920</DescriptorUI>"
    "<DescriptorName><String>Hypoglycemia</String></DescriptorName>"
    "<ConceptList>"
    '<Concept PreferredConceptYN="Y"><ConceptUI>M0000001</ConceptUI>'
    "<ConceptName><String>Hypoglycemia</String></ConceptName>"
    "<TermList>"
    '<Term ConceptPreferredTermYN="Y" IsPermutedTermYN="N" RecordPreferredTermYN="Y">'
    "<TermUI>T000010</TermUI><String>Hypoglycemia</String></Term>"
    "</TermList></Concept></ConceptList></DescriptorRecord>"
    "</DescriptorRecordSet>"
)
_EXPECTED = [
    ("D003924", "Diabetes Mellitus", "name", True, "M0006000", None),
    ("D003924", "Diabetes", "entry", False, "M0006000", None),
    ("D003924", "Mellitus, Diabetes", "entry", False, "M0006000", None),
    ("D003924", "Sugar Diabetes", "entry", False, "M0006001", "NRW"),
    ("D003920", "Hypoglycemia", "name", True, "M0000001", None),
]


def test_parse_mesh_terms(tmp_path):
    path = tmp_path / "desc2026.xml"
    path.write_text(_XML, encoding="utf-8")
    assert _parse_mesh_terms(path) == _EXPECTED


def test_parse_mesh_terms_gzip(tmp_path):
    path = tmp_path / "desc2026.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(_XML.encode("utf-8"))
    assert _parse_mesh_terms(path) == _EXPECTED


def test_parse_mesh_terms_malformed_gives_empty(tmp_path):
    path = tmp_path / "desc2026.xml"
    path.write_text("<DescriptorRecordSet><DescriptorRecord>", encoding="utf-8")
    assert _parse_mesh_terms(path) == []


def test_mesh_release_of():
    assert mesh_release_of("mesh/desc2026.gz") == "2026"
    assert mesh_release_of("desc2027.xml") == "2027"
    assert mesh_release_of("x.xml") is None


def _seed(pg_conn, files):
    with pg_conn.cursor() as cur:
        for i, f in enumerate(files):
            cur.execute(
                "INSERT INTO episteme.articles (id, source, source_file, source_record_id) "
                "VALUES (%s, 'mesh', %s, %s)",
                (f"mesh:D00392{i}", f, f"D00392{i}"),
            )
    pg_conn.commit()


@pytest.mark.pg
def test_build_fills_mesh_term_and_replaces_on_new_release(pg_conn, tmp_path):
    from episteme.data.graph_builder import build

    from ._graph_schema import setup_graph_schema

    setup_graph_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0017.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0018.read_text(encoding="utf-8"))
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "desc2026.xml").write_text(_XML, encoding="utf-8")
    _seed(pg_conn, ["desc2026.xml"])

    res = build(pg_conn, source="mesh", raw_dir=raw, run_id="t1")
    pg_conn.commit()
    assert res["mesh_term"] == len(_EXPECTED)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT term, term_norm, term_kind, is_preferred, concept_ui, concept_relation, "
            "mesh_release, source_file FROM episteme.mesh_term ORDER BY term, descriptor_ui"
        )
        rows = cur.fetchall()
    assert len(rows) == len(_EXPECTED)
    for term, norm, _kind, _pref, _cui, _rel, release, src in rows:
        assert norm == term_norm(term)
        assert release == "2026" and src == "desc2026.xml"
    assert ("Sugar Diabetes", "sugar diabetes", "entry", False, "M0006001", "NRW") in [
        r[:6] for r in rows
    ]

    # A newer release replaces every row.
    (raw / "desc2027.xml").write_text(_XML, encoding="utf-8")
    _seed(pg_conn, ["desc2027.xml"])
    build(pg_conn, source="mesh", raw_dir=raw, run_id="t2")
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT mesh_release FROM episteme.mesh_term")
        assert cur.fetchall() == [("2027",)]
        cur.execute("SELECT count(*) FROM episteme.mesh_term")
        assert cur.fetchone()[0] == len(_EXPECTED)


@pytest.mark.pg
def test_build_without_mesh_term_table_warns_and_still_builds(pg_conn, tmp_path, capsys):
    from episteme.data.graph_builder import build

    from ._graph_schema import setup_graph_schema

    setup_graph_schema(pg_conn)  # no 0018: mesh_term does not exist
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "desc2026.xml").write_text(_XML, encoding="utf-8")
    _seed(pg_conn, ["desc2026.xml"])
    res = build(pg_conn, source="mesh", raw_dir=raw, run_id="t0")
    pg_conn.commit()
    assert res["mesh_term"] == 0
    assert "apply migration 0018" in capsys.readouterr().err


@pytest.mark.pg
def test_build_warns_when_raw_file_is_missing(pg_conn, tmp_path, capsys):
    from episteme.data.graph_builder import build

    from ._graph_schema import setup_graph_schema

    setup_graph_schema(pg_conn)
    raw = tmp_path / "raw"
    raw.mkdir()
    _seed(pg_conn, ["desc2026.xml"])
    res = build(pg_conn, source="mesh", raw_dir=raw, run_id="t3")
    pg_conn.commit()
    assert res["mesh_missing_raw"] == 1
    assert (
        f"WARNING: graph_builder mesh phase: raw file for desc2026.xml not found under {raw}; "
        "set EPISTEME_RAW_ROOT"
    ) in capsys.readouterr().err
