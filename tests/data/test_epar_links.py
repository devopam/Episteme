"""EU label substance links (``episteme.data.ema.epar_links``), method ``ema_name_exact``.

The non-pg tests cover the split and the decision. The pg-marked tests run against
``episteme_test`` only (the controller runs them); their schema is built once per module,
with migration 0015 for ``episteme_api_internal.norm_name``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from psycopg import sql

from episteme.data.ema import epar_links as el
from episteme.data.ema import extract_epar as ex

from .test_epar import _clear_settings, doc, make_pdf


# ---------------------------------------------------------------------------
# non-pg
# ---------------------------------------------------------------------------
def test_split_active_substance():
    assert el.split_active_substance("levodopa;carbidopa") == ["levodopa", "carbidopa"]
    assert el.split_active_substance(" Levodopa ; ; carbidopa ;") == ["Levodopa", "carbidopa"]
    assert el.split_active_substance("a|b") == ["a|b"]  # only ';' separates
    assert el.split_active_substance(None) == [] and el.split_active_substance("") == []
    assert el.split_active_substance("x; x") == ["x"]


def _doc_row(article: str, *, category: str = "Human", substance: str = "levodopa", **kw):
    return {
        "article_id": article,
        "has_document": True,
        "doc_type": el.LABEL_DOC_TYPE,
        "product_number": "EMEA/H/C/000001",
        "has_medicine": True,
        "category": category,
        "active_substance": substance,
        **kw,
    }


def test_vet_not_linked():
    rows, report = el.decide(
        [_doc_row("ema_epar:1", category="Veterinary")], {"levodopa": (1, "UNIILEVO")}
    )
    assert rows == []
    assert [(r["article_id"], r["reason"]) for r in report] == [("ema_epar:1", "veterinary")]


def test_decide_links_each_part_and_reports_the_rest():
    docs = [
        _doc_row("ema_epar:1", substance="levodopa;carbidopa"),
        _doc_row("ema_epar:2", substance="ambiguine"),
        _doc_row("ema_epar:3", substance="influenza vaccine"),
        _doc_row("ema_epar:4", substance="levodopa;nouniiname"),
        _doc_row("ema_epar:5", doc_type="assessment-report"),  # not a label: skipped
        _doc_row("ema_epar:6", has_document=False, doc_type=None),
        _doc_row("ema_epar:7", has_medicine=False, category=None, substance=None),
    ]
    matches = {
        "levodopa": (1, "UNIILEVO"),
        "carbidopa": (1, "UNIICARB"),
        "ambiguine": (2, None),
        "nouniiname": (1, None),
    }
    rows, report = el.decide(docs, matches)
    assert rows == [
        ("ema_epar:1", "UNIICARB"),
        ("ema_epar:1", "UNIILEVO"),
        ("ema_epar:4", "UNIILEVO"),
    ]
    by_id = {r["article_id"]: r for r in report}
    assert {k: v["reason"] for k, v in by_id.items()} == {
        "ema_epar:2": "ambiguous",
        "ema_epar:3": "no_match",
        "ema_epar:4": "partial",
        "ema_epar:6": "no_document_record",
        "ema_epar:7": "no_medicine_record",
    }
    assert by_id["ema_epar:4"]["parts"] == {"nouniiname": "no_unii"}


def test_the_link_step_is_in_the_load_wrapper_after_retire():
    root = Path(__file__).resolve().parents[2] / "scripts" / "data" / "ema"
    text = (root / "load_ema_epar.sh").read_text(encoding="utf-8")
    assert text.index("-m episteme.data.ema.retire") < text.index("-m episteme.data.ema.epar_links")
    assert 'exec "$PY" -m episteme.data.ema.retire' not in text


def test_the_gate_hashes_the_link_module():
    from episteme.ops import gate

    assert "episteme.data.ema.epar_links" in gate.hash_modules("ema_epar", "load")


def test_the_report_is_written_next_to_the_run_markers(tmp_path):
    path = el.write_report(tmp_path, "r1", {"linked_rows": 0, "unlinked": []})
    assert path == tmp_path / "_ops" / "ema_epar" / "runs" / "label_links_r1.json"
    payload = json.loads(path.read_text("utf-8"))
    assert payload["method"] == "ema_name_exact" and payload["run_id"] == "r1"


# ---------------------------------------------------------------------------
# pg (run by the controller, not in the cloud sandbox)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DIR = REPO_ROOT / "src/episteme/data/db"
MIGRATIONS = DB_DIR / "migrations"
# The migrations the link step reads after schema.sql (0015: norm_name).
_MIGRATION_FILES = (
    "0002_container_and_book_parts.sql",
    "0006_pubmed_graph.sql",
    "0010_regulatory_sources.sql",
    "0011_label_section_ordinal.sql",
    "0014_article_text_virtual.sql",
    "0015_api_layer.sql",
)
RELEASE = "2026-11"


@pytest.fixture(scope="module")
def _schema():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    if conninfo_to_dict(dsn).get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to DROP SCHEMA")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
            cur.execute((DB_DIR / "extensions.sql").read_text("utf-8"))
            cur.execute((DB_DIR / "schema.sql").read_text("utf-8"))
            for name in _MIGRATION_FILES:
                cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def _add_release(conn, dataset: str, rows: list[tuple[str, dict]]) -> None:
    from episteme.data.regulatory.load_reg import partition_names

    ds_name, leaf = partition_names(dataset, RELEASE)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {ds} PARTITION OF episteme.reg_record "
                "FOR VALUES IN ({v}) PARTITION BY LIST (release_id)"
            ).format(ds=sql.Identifier("episteme", ds_name), v=sql.Literal(dataset))
        )
        cur.execute(
            sql.SQL("CREATE TABLE {leaf} PARTITION OF {ds} FOR VALUES IN ({v})").format(
                leaf=sql.Identifier("episteme", leaf),
                ds=sql.Identifier("episteme", ds_name),
                v=sql.Literal(RELEASE),
            )
        )
        for key, payload in rows:
            cur.execute(
                "INSERT INTO episteme.reg_record (dataset, release_id, record_key, jurisdiction, "
                "payload, source_file) VALUES (%s, %s, %s, 'EU', %s::jsonb, %s)",
                (dataset, RELEASE, key, json.dumps(payload), f"{dataset}.json"),
            )
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES (%s, %s, %s, 'loaded', true)",
            (dataset, RELEASE, len(rows)),
        )


def _medicine(n: int, substance: str, category: str = "Human") -> tuple[str, dict]:
    pn = f"EMEA/H/C/{n:06d}"
    return pn, {"ema_product_number": pn, "category": category, "active_substance": substance}


# Documents 1-4 are labels; 5 is an assessment report of product 1 (never linked).
_MEDICINES = [
    _medicine(1, "Levodopa; Carbidopa"),
    _medicine(2, "Ambiguine"),
    _medicine(3, "Influenza vaccine (surface antigen, inactivated)"),
    _medicine(4, "Levodopa", "Veterinary"),
]


@pytest.fixture
def db(_schema, pg_conn, clean_substance, monkeypatch, tmp_path):
    from psycopg import sql as _sql

    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "audit_root"))
    monkeypatch.setenv("EPISTEME_RUN_ID", "links-test")
    _clear_settings()
    conn = pg_conn
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'episteme.reg_record'::regclass"
        )
        for (name,) in cur.fetchall():
            cur.execute(_sql.SQL("DROP TABLE {} CASCADE").format(_sql.Identifier("episteme", name)))
        cur.execute(
            "TRUNCATE episteme.reg_release, episteme.label_section, episteme.label_substance, "
            "episteme.substance_xref, episteme.substance RESTART IDENTITY"
        )
        cur.execute("DELETE FROM episteme.article_body WHERE source = 'ema_epar'")
        cur.execute("DELETE FROM episteme.articles WHERE source = 'ema_epar'")
        cur.execute(
            "INSERT INTO episteme.substance (substance_key, unii, preferred_name, moiety_key) "
            "VALUES (1, 'UNIILEVO', 'Levodopa', 1), (2, 'UNIICARB', 'Carbidopa', 2), "
            "(3, 'UNIIAMB3', 'Ambiguine A', 3), (4, 'UNIIAMB4', 'Ambiguine B', 4)"
        )
        cur.execute(
            "INSERT INTO episteme.substance_xref "
            "(substance_key, id_type, id_value, method, confidence, source) VALUES "
            "(1, 'name', 'levodopa', 'normalised_name', 1.0, 'fda_substance'), "
            "(1, 'ema_name', 'levodopa', 'normalised_name', 1.0, 'ema'), "
            "(2, 'name', 'carbidopa', 'normalised_name', 1.0, 'fda_substance'), "
            "(3, 'name', 'ambiguine', 'normalised_name', 1.0, 'fda_substance'), "
            "(4, 'name', 'ambiguine', 'normalised_name', 1.0, 'fda_substance')"
        )
    _add_release(conn, "ema_medicines", _MEDICINES)
    docs = [doc(i) for i in range(1, 5)] + [doc(5, type_="assessment-report")]
    docs[4]["ema_product_number"] = "EMEA/H/C/000001"
    _add_release(conn, "ema_epar_documents", [(d["id"], d) for d in docs])
    conn.commit()
    yield conn


def _extract_and_load(conn, tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    (raw / "epar").mkdir(parents=True, exist_ok=True)
    docs = [doc(i) for i in range(1, 5)] + [doc(5, type_="assessment-report")]
    body = {"meta": {"total_records": len(docs)}, "data": docs}
    (raw / "ema_epar_documents.json").write_text(json.dumps(body), encoding="utf-8")
    for i in range(1, 6):
        (raw / "epar" / f"{i}.pdf").write_bytes(make_pdf([f"Text of document {i}."]))
    out = tmp_path / "out"
    ex.extract_epar(raw, out)
    (shard,) = sorted((out / "staging" / "ema_epar").glob("*.parquet"))
    _load(conn, shard)
    return shard


def _load(conn, shard: Path) -> None:
    from episteme.data import load_articles, postgres_loader

    load_articles._pre_load(conn, "ema_epar", shard)
    postgres_loader.load_source_file(conn, source="ema_epar", staging_path=shard, run_id="t")
    conn.commit()


def _links(conn) -> list[tuple[str, str, str | None]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT article_id, unii, ndc_product FROM episteme.label_substance "
            "ORDER BY article_id, unii"
        )
        return cur.fetchall()


@pytest.mark.pg
def test_link_exact_single_match(db, tmp_path):
    _extract_and_load(db, tmp_path)
    rows, report = el.link_rows(db, None)
    assert rows == [("ema_epar:1", "UNIICARB"), ("ema_epar:1", "UNIILEVO")]
    result = el.write_links(db, processed_dir=tmp_path / "out")
    assert result["linked_rows"] == 2 and result["linked_documents"] == 1
    assert result["label_documents"] == 4
    assert result["releases"] == {"ema_medicines": RELEASE, "ema_epar_documents": RELEASE}
    assert _links(db) == [("ema_epar:1", "UNIICARB", None), ("ema_epar:1", "UNIILEVO", None)]
    payload = json.loads(Path(result["report_path"]).read_text("utf-8"))
    assert payload["method"] == "ema_name_exact"
    # The veterinary label is loaded as text and reported, never linked.
    assert {r["article_id"]: r["reason"] for r in report}["ema_epar:4"] == "veterinary"


@pytest.mark.pg
def test_link_ambiguous_reported(db, tmp_path):
    _extract_and_load(db, tmp_path)
    _, report = el.link_rows(db, None)
    entry = {r["article_id"]: r for r in report}["ema_epar:2"]
    assert entry["reason"] == "ambiguous" and entry["parts"] == {"Ambiguine": "ambiguous"}


@pytest.mark.pg
def test_vaccine_unmatched_reported(db, tmp_path):
    _extract_and_load(db, tmp_path)
    rows, report = el.link_rows(db, None)
    assert all(a != "ema_epar:3" for a, _ in rows)
    entry = {r["article_id"]: r for r in report}["ema_epar:3"]
    assert entry["reason"] == "no_match"
    assert entry["product_number"] == "EMEA/H/C/000003"
    # The assessment report (document 5) is neither linked nor reported.
    assert "ema_epar:5" not in {r["article_id"] for r in report}


@pytest.mark.pg
def test_links_idempotent_on_reload(db, tmp_path):
    shard = _extract_and_load(db, tmp_path)
    el.write_links(db, processed_dir=tmp_path / "out")
    first = _links(db)
    el.write_links(db, processed_dir=tmp_path / "out")
    assert _links(db) == first
    # A reload of the part deletes the links of its articles (replace_labels); the link
    # step after the load writes the same rows again.
    _load(db, shard)
    assert _links(db) == []
    el.write_links(db, processed_dir=tmp_path / "out")
    assert _links(db) == first


@pytest.mark.pg
def test_a_missing_release_refuses_before_deleting(db, tmp_path):
    _extract_and_load(db, tmp_path)
    el.write_links(db, processed_dir=tmp_path / "out")
    before = _links(db)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme.reg_release SET is_latest = false WHERE dataset = 'ema_medicines'"
        )
    db.commit()
    with pytest.raises(el.EparLinkError, match="ema_medicines"):
        el.write_links(db, processed_dir=tmp_path / "out")
    db.rollback()
    assert _links(db) == before
