"""MeSH qualifier parse (non-pg) and the mesh graph phase filling mesh_qualifier (pg)."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from episteme.data.graph_builder import _parse_mesh_qualifiers

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0017 = REPO_ROOT / "src/episteme/data/db/migrations/0017_api_no_timeouts.sql"

_QUALIFIER_AE = (
    "<AllowableQualifier><QualifierReferredTo><QualifierUI>Q000009</QualifierUI>"
    "<QualifierName><String>adverse effects</String></QualifierName></QualifierReferredTo>"
    "<Abbreviation>AE</Abbreviation></AllowableQualifier>"
)
_QUALIFIER_MO = (
    "<AllowableQualifier><QualifierReferredTo><QualifierUI>Q000401</QualifierUI>"
    "<QualifierName><String>mortality</String></QualifierName></QualifierReferredTo>"
    "<Abbreviation>MO</Abbreviation></AllowableQualifier>"
)
_XML = (
    "<DescriptorRecordSet>"
    "<DescriptorRecord><DescriptorUI>D003924</DescriptorUI>"
    "<DescriptorName><String>Diabetes Mellitus</String></DescriptorName>"
    "<TreeNumberList><TreeNumber>C18.452.394.750</TreeNumber></TreeNumberList>"
    f"<AllowableQualifiersList>{_QUALIFIER_AE}{_QUALIFIER_MO}</AllowableQualifiersList>"
    "</DescriptorRecord>"
    "<DescriptorRecord><DescriptorUI>D003920</DescriptorUI>"
    "<DescriptorName><String>Diabetes Mellitus, Type 2</String></DescriptorName>"
    "<TreeNumberList><TreeNumber>C18.452.394.750.149</TreeNumber></TreeNumberList>"
    f"<AllowableQualifiersList>{_QUALIFIER_AE}</AllowableQualifiersList>"
    "</DescriptorRecord>"
    "</DescriptorRecordSet>"
)
_EXPECTED = {"Q000009": "adverse effects", "Q000401": "mortality"}


def test_parse_mesh_qualifiers(tmp_path):
    path = tmp_path / "desc2026.xml"
    path.write_text(_XML, encoding="utf-8")
    assert _parse_mesh_qualifiers(path) == _EXPECTED


def test_parse_mesh_qualifiers_gzip(tmp_path):
    path = tmp_path / "desc2026.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(_XML.encode("utf-8"))
    assert _parse_mesh_qualifiers(path) == _EXPECTED


def test_parse_mesh_qualifiers_malformed_gives_empty(tmp_path):
    path = tmp_path / "desc2026.xml"
    path.write_text("<DescriptorRecordSet><DescriptorRecord>", encoding="utf-8")
    assert _parse_mesh_qualifiers(path) == {}


@pytest.mark.pg
def test_build_populates_mesh_qualifier(pg_conn, tmp_path):
    from episteme.data.graph_builder import build

    from ._graph_schema import setup_graph_schema

    setup_graph_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0017.read_text(encoding="utf-8"))
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "desc2026.xml").write_text(_XML, encoding="utf-8")
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, source_record_id) "
            "VALUES (%s, 'mesh', %s, %s), (%s, 'mesh', %s, %s)",
            ("mesh:D003924", "desc2026.xml", "D003924", "mesh:D003920", "desc2026.xml", "D003920"),
        )
    pg_conn.commit()

    for run_id in ("q1", "q2"):  # the second build must not duplicate rows
        res = build(pg_conn, source="mesh", raw_dir=raw, run_id=run_id)
        pg_conn.commit()
        assert res["mesh_qualifier"] == 2
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT qualifier_ui, qualifier_name, source_file FROM episteme.mesh_qualifier "
                "ORDER BY qualifier_ui"
            )
            assert cur.fetchall() == [
                ("Q000009", "adverse effects", "desc2026.xml"),
                ("Q000401", "mortality", "desc2026.xml"),
            ]


@pytest.mark.pg
def test_build_without_mesh_qualifier_table_warns_and_still_builds(pg_conn, tmp_path, capsys):
    from episteme.data.graph_builder import build

    from ._graph_schema import setup_graph_schema

    setup_graph_schema(pg_conn)  # no 0017: mesh_qualifier does not exist
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "desc2026.xml").write_text(_XML, encoding="utf-8")
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, source_record_id) "
            "VALUES (%s, 'mesh', %s, %s)",
            ("mesh:D003924", "desc2026.xml", "D003924"),
        )
    pg_conn.commit()
    res = build(pg_conn, source="mesh", raw_dir=raw, run_id="q0")
    pg_conn.commit()
    assert res["mesh_qualifier"] == 0 and res["mesh_tree"] == 2
    assert "0017" in capsys.readouterr().err
