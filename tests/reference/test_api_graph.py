"""graph_neighbours as a SQL function (migration 0015) and its Python wrapper
``graph.neighbours`` (pg: episteme_test only), and the shared_mesh scan cap as the
synced family 'graph_scan' (non-pg)."""

from __future__ import annotations

import json
import re

import pytest
from psycopg import sql

from episteme import config
from episteme.config import api_result_limits
from episteme.data import graph_builder
from episteme.reference import api_settings
from episteme.reference.graph import neighbours

from ._pbrer_db import (
    DB_DIR,
    KW,
    MIGRATIONS,
    TEST_AACT_RELEASE,
    TEST_AACT_SCHEMA,
    grant_owner,
)

_AACT_ZIP = f"aact/{TEST_AACT_RELEASE}_export_ctgov.zip"
_KINDS_NOTE = "allowed kinds: cites, cited_by, shared_mesh, trial, label"


# ---------------------------------------------------------------------------
# non-pg
# ---------------------------------------------------------------------------
def test_graph_scan_is_a_synced_family(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EPISTEME_API_LIMIT_GRAPH_SCAN", raising=False)
    assert config.API_RESULT_LIMITS["graph_scan"] == (20000, 20000)
    written: list[tuple] = []

    class Cur:
        def execute(self, query, params=None):
            pass

        def executemany(self, query, rows):
            if "result_limit" in query:
                written.extend(rows)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def transaction(self):
            return Cur()

        def cursor(self):
            return Cur()

    api_settings.sync(Conn())
    assert ("graph_scan", 20000, 20000) in written


@pytest.mark.parametrize(
    "path", [MIGRATIONS / "0015_api_layer.sql", DB_DIR / "schema.sql"], ids=["0015", "schema"]
)
def test_shared_mesh_cap_read_from_settings(path):
    text = path.read_text(encoding="utf-8")
    start = text.index("CREATE OR REPLACE FUNCTION episteme_api_internal.graph_neighbours(")
    body = text[start : text.index("FUNCTION episteme_api.graph_neighbours(", start)]
    assert "limit_for('graph_scan')" in body
    assert not re.search(r"\b\d{4,}\b", body)  # no hard-coded cap


# ---------------------------------------------------------------------------
# pg
# ---------------------------------------------------------------------------
@pytest.fixture
def db(ref_db):
    """The ref_db substances (1 metformin, 2 its salt, 3 aspirin), the API settings,
    a 'pubmed' release and the articles the edges point at."""
    api_settings.sync(ref_db)
    with ref_db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('pubmed', '2026', 1, 'loaded', true)"
        )
        cur.execute(
            "INSERT INTO episteme.articles (id, source, pmid, year) VALUES "
            "('pubmed:100', 'pubmed', '100', 2020), ('pmc:PMC100', 'pmc', '100', 2020), "
            "('pubmed:200', 'pubmed', '200', 2021), ('pubmed:300', 'pubmed', '300', 0), "
            "('pubmed:400', 'pubmed', '400', 2023)"
        )
        cur.execute(
            "INSERT INTO episteme.articles (id, source, year) VALUES ('pmc:PMC9', 'pmc', 2022)"
        )
    ref_db.commit()
    yield ref_db


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _internal(conn, article_id, kind, limit=None):
    return _rows(
        conn,
        "SELECT * FROM episteme_api_internal.graph_neighbours(%s, %s, %s)",
        (article_id, kind, limit),
    )


def _ids(rows) -> list[str]:
    return [r[1] for r in rows if r[0] == "article"]


def _notes(rows) -> list[str]:
    return [r[9] for r in rows if r[0] == "note"]


def _cites(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.article_cites (src_pmid, dst_pmid, source_file) VALUES "
            "('100', '200', 'cites/a.xml'), ('100', '300', 'cites/b.xml'), "
            "('100', '300', 'cites/a.xml'), ('400', '100', 'cites/c.xml'), "
            "('200', '300', 'cites/a.xml')"
        )
    conn.commit()


@pytest.mark.pg
def test_cites_matches_graph_builder(db):
    assert neighbours(db, "pubmed:100", "cites", **KW).coverage.missing == ("article_cites",)
    _cites(db)
    for pmid in ("100", "200", "300", "400"):
        for kind in ("cites", "cited_by"):
            expected = graph_builder.neighbours(db, pmid, 1, kind)
            res = neighbours(db, f"pubmed:{pmid}", kind, **KW)
            assert sorted(c.value for c in res.claims) == expected, (pmid, kind)
            assert res.coverage.notes == () and res.coverage.missing == ()
    res = neighbours(db, "pubmed:100", "cites", **KW)
    assert [c.value for c in res.claims] == ["200", "300"]
    first, second = res.claims
    assert first.provenance.record_key == "pmid:200"
    assert first.provenance.source == "pubmed"
    assert first.provenance.release == "2026"
    assert first.provenance.source_file == "cites/a.xml"
    assert first.method == "fetched" and first.confidence == 1.0
    assert "(2021)" in first.text
    assert "(year unknown)" in second.text  # year 0 is unknown
    assert second.provenance.source_file == "cites/a.xml"  # the smallest of the edge rows
    # Another id of the same PMID, 'pmid:' and a bare PMID give the same neighbours.
    for alias in ("pmc:PMC100", "pmid:100", "100", "PubMed:100", "PMID:100"):
        assert _ids(_internal(db, alias, "cites")) == ["pubmed:200", "pubmed:300"]
    assert _ids(_internal(db, "pubmed:300", "cited_by")) == ["pubmed:100", "pubmed:200"]
    assert {r[6] for r in _internal(db, "pubmed:300", "cited_by")} == {"cited_by"}


def _aact(conn) -> None:
    s = sql.Identifier(TEST_AACT_SCHEMA)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(s))
        cur.execute(sql.SQL("CREATE TABLE {}.studies (nct_id text, brief_title text)").format(s))
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.study_references (id text, nct_id text, pmid text, "
                "reference_type text, citation text)"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.studies VALUES ('NCT01', 'Metformin in X'), "
                "('NCT02', ''), ('NCT03', 'Aspirin in Z')"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.study_references VALUES "
                "('1', 'NCT02', '100', 'RESULT', 'c1'), ('2', 'NCT01', '100', 'BACKGROUND', 'c2'), "
                "('3', 'NCT01', '100', 'RESULT', 'c3'), ('4', 'NCT03', '200', 'RESULT', 'c4'), "
                "('5', 'NCT03', NULL, 'RESULT', 'c5')"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}._load_state (table_name text PRIMARY KEY, "
                "fingerprint text NOT NULL, source_file text NOT NULL, rows bigint NOT NULL, "
                "stats jsonb NOT NULL, done_at timestamptz NOT NULL DEFAULT now())"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}._load_state (table_name, fingerprint, source_file, rows, "
                "stats) VALUES ('_release', 'fp', %s, 3, '{{}}'::jsonb)"
            ).format(s),
            (_AACT_ZIP,),
        )
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('aact', %s, 3, 'loaded', true)",
            (TEST_AACT_RELEASE,),
        )
    conn.commit()
    grant_owner(conn)


@pytest.mark.pg
def test_trial_by_pmid(db):
    # No AACT release: a missing row.
    assert neighbours(db, "pubmed:100", "trial", **KW).coverage.missing == ("aact",)
    _aact(db)
    res = neighbours(db, "pubmed:100", "trial", **KW)
    assert [c.value for c in res.claims] == ["NCT01", "NCT02"]  # one claim per study
    c = res.claims[0]
    assert c.provenance.source == "aact"
    assert c.provenance.record_key == "aact:NCT01"
    assert c.provenance.release == TEST_AACT_RELEASE
    assert c.provenance.source_file == _AACT_ZIP
    assert "Metformin in X" in c.text
    rows = _internal(db, "pubmed:100", "trial")
    assert _ids(rows) == ["nct:NCT01", "nct:NCT02"]
    assert [r[3] for r in rows] == ["Metformin in X", None]  # an empty title is NULL
    assert {r[6] for r in rows} == {"trial"}
    assert _ids(_internal(db, "pmc:PMC100", "trial")) == ["nct:NCT01", "nct:NCT02"]
    assert _ids(_internal(db, "pubmed:200", "trial")) == ["nct:NCT03"]
    assert _internal(db, "pubmed:400", "trial") == []
    # An article without a PMID, and an unknown id: one note, no lookup.
    for article_id in ("pmc:PMC9", "nope:1"):
        res = neighbours(db, article_id, "trial", **KW)
        assert res.claims == ()
        assert res.coverage.notes == (f"article {article_id} has no PMID; kind trial needs one",)


def _labels(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.article_chemicals "
            "(pmid, substance_ui, name, registry_number, source_file) VALUES "
            "('100', 'D008687', 'Metformin', '9100L32L2N', 'pubmed/a.xml'), "
            "('100', NULL, 'Hypoglycemic Agents', '0', 'pubmed/a.xml'), "
            "('200', 'D001241', '  ASPIRIN ', 'R16CO5Y76E', 'pubmed/b.xml')"
        )
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, year, book_meta) VALUES "
            "('dailymed:set1', 'dailymed', 'dailymed/part-1.parquet', 2024, %s::jsonb), "
            "('ema_epar:100', 'ema_epar', 'ema_epar/part-1.parquet', 2023, %s::jsonb)",
            (json.dumps({"version": "7"}), json.dumps({"last_updated_date": "2025-06-01"})),
        )
        cur.execute(
            "INSERT INTO episteme.label_substance (article_id, unii) VALUES "
            "('dailymed:set1', 'UNIIMET01'), ('dailymed:set1', 'UNIIMET02'), "
            "('dailymed:set2', 'UNIIMET02'), ('ema_epar:100', 'UNIIMET01'), "
            "('dailymed:set3', 'UNIIASP03')"
        )
    conn.commit()


@pytest.mark.pg
def test_label_via_chemicals(db):
    # No chemicals at all, then no label link at all: a missing row each.
    assert neighbours(db, "pubmed:100", "label", **KW).coverage.missing == ("article_chemicals",)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.article_chemicals (pmid, name) VALUES ('900', 'Metformin')"
        )
    db.commit()
    assert neighbours(db, "pubmed:100", "label", **KW).coverage.missing == ("dailymed",)
    _labels(db)
    res = neighbours(db, "pubmed:100", "label", **KW)
    # 'Metformin' names substance 1; its moiety family holds the salt (substance 2), so
    # set2, which lists only the salt UNII, is found too.
    assert [c.provenance.record_key for c in res.claims] == [
        "dailymed:set1",
        "dailymed:set2",
        "ema_epar:100",
    ]
    us, _, eu = res.claims
    assert us.provenance.source == "dailymed" and us.provenance.release == "7"
    assert us.provenance.source_file == "dailymed/part-1.parquet"
    assert us.method == "mapped"
    assert eu.provenance.source == "ema_epar" and eu.provenance.release == "2025-06-01"
    assert "(2023)" in eu.text
    # Case and white space of the chemical name do not matter; a label without an
    # articles row still answers, with no release.
    res = neighbours(db, "pubmed:200", "label", **KW)
    assert [c.provenance.record_key for c in res.claims] == ["dailymed:set3"]
    assert res.claims[0].provenance.release is None
    assert {r[6] for r in _internal(db, "pubmed:200", "label")} == {"label"}
    # No chemicals: no neighbours and no note.
    res = neighbours(db, "pubmed:300", "label", **KW)
    assert res.claims == () and res.coverage.notes == () and res.coverage.missing == ()


def _mesh(conn) -> None:
    rows = [
        # The seed 100: major D1, D2 and D4; D3 is not a major topic.
        ("100", "D1", True, "m/a"),
        ("100", "D2", True, "m/a"),
        ("100", "D4", True, "m/a"),
        ("100", "D3", False, "m/a"),
        # D1: 2 articles (300 twice, from two files).
        ("300", "D1", False, "m/c"),
        ("300", "D1", False, "m/b"),
        # D2: 5 articles.
        ("200", "D2", False, "m/b"),
        ("300", "D2", True, "m/b"),
        ("400", "D2", True, "m/b"),
        ("500", "D2", False, "m/b"),
        # D4: 3 articles.
        ("600", "D4", False, "m/d"),
        ("700", "D4", True, "m/d"),
        # D3 is shared with 800 only: not a major topic of the seed, so not used.
        ("800", "D3", True, "m/e"),
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO episteme.article_mesh "
            "(pmid, descriptor_ui, descriptor_name, major_topic, source_file) "
            "VALUES (%s, %s, %s, %s, %s)",
            [(p, d, f"name {d}", major, sf) for p, d, major, sf in rows],
        )
    conn.commit()


@pytest.mark.pg
def test_shared_mesh_rare_first(db):
    assert neighbours(db, "pubmed:100", "shared_mesh", **KW).coverage.missing == ("article_mesh",)
    _mesh(db)
    rows = _internal(db, "pubmed:100", "shared_mesh")
    # 300: 1/2 (D1) + 1/5 (D2); 600 and 700: 1/3 (D4); 200, 400, 500: 1/5 (D2).
    assert _ids(rows) == [
        "pubmed:300",
        "pubmed:600",
        "pubmed:700",
        "pubmed:200",
        "pubmed:400",
        "pubmed:500",
    ]
    scores = [r[5] for r in rows if r[0] == "article"]
    assert scores == pytest.approx([0.7, 1 / 3, 1 / 3, 0.2, 0.2, 0.2], rel=1e-6)
    assert _notes(rows) == []
    assert {r[6] for r in rows} == {"shared_mesh"}
    assert rows[0][10] == "m/b"  # the smallest source_file of the shared rows
    # The Python claims keep the order; the score is in the text, confidence is 1.
    res = neighbours(db, "100", "shared_mesh", **KW)
    assert [c.value for c in res.claims] == ["300", "600", "700", "200", "400", "500"]
    assert res.claims[0].confidence == 1.0 and res.claims[0].method == "computed"
    assert "score 0.7" in res.claims[0].text
    # The cap (family graph_scan): D2 is on 5 article_mesh rows, more than 4: it is not
    # used, and a note says so.
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 4, max_limit = 4 "
            "WHERE family = 'graph_scan'"
        )
    db.commit()
    capped = _internal(db, "pubmed:100", "shared_mesh")
    assert _ids(capped) == ["pubmed:300", "pubmed:600", "pubmed:700"]
    assert _notes(capped) == [
        "1 of 3 major-topic descriptors not used: each is on more than 4 article_mesh rows"
    ]
    api_settings.sync(db)
    # p_limit keeps the top of the order.
    assert _ids(_internal(db, "pubmed:100", "shared_mesh", limit=2)) == [
        "pubmed:300",
        "pubmed:600",
    ]
    # An article with no major-topic descriptor: a note.
    assert _notes(_internal(db, "pubmed:200", "shared_mesh")) == [
        "article pubmed:200 has no major-topic MeSH descriptor"
    ]


@pytest.mark.pg
def test_unknown_kind_note(db):
    res = neighbours(db, "pubmed:100", "siblings", **KW)
    assert res.claims == ()
    assert res.coverage.notes == (f"unknown kind siblings; {_KINDS_NOTE}",)
    assert res.coverage.missing == ()
    # The kind is checked before the article id.
    assert _notes(_internal(db, "pmc:PMC9", None)) == [f"unknown kind (none); {_KINDS_NOTE}"]
    # The kind is trimmed and case-insensitive.
    _cites(db)
    assert _ids(_internal(db, "pubmed:100", " CITES ")) == ["pubmed:200", "pubmed:300"]


@pytest.mark.pg
def test_public_graph_truncates(db):
    limit = api_result_limits()["graph"][0]
    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO episteme.article_cites (src_pmid, dst_pmid, source_file) "
            "VALUES ('1', %s, 'cites/x.xml')",
            [(str(10_000 + i),) for i in range(limit + 5)],
        )
    db.commit()
    internal = _rows(db, "SELECT * FROM episteme_api_internal.graph_neighbours('1', 'cites')")
    public = _rows(db, "SELECT * FROM episteme_api.graph_neighbours('1', 'cites')")
    assert len(_ids(internal)) == limit + 5
    assert _ids(public) == _ids(internal)[:limit]
    assert _notes(public) == [f"truncated: {limit} of more than {limit}"]
