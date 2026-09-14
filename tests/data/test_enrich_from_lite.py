"""``pg`` tests for the Europe PMC lite_metadata -> episteme.articles
enrichment pass. ``episteme.articles`` is already in ``db/schema.sql`` (not a
migration), so the plain ``_setup_schema`` rebuild below gets it for free.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "lite_metadata" / "sample.tgz"


def _setup_schema(conn):
    with (
        conn.cursor() as cur,
        open(REPO_ROOT / "src/episteme/data/db/extensions.sql") as ext,
        open(REPO_ROOT / "src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    conn.commit()


def _seed_article(
    cur, *, id_, pmcid, journal=None, year=None, extract_status="ok", source="pubmed"
):
    cur.execute(
        """
        INSERT INTO episteme.articles
            (id, source, source_file, pmcid, journal, year, extract_status)
        VALUES (%s, %s, 'seed.xml', %s, %s, %s, %s)
        """,
        (id_, source, pmcid, journal, year, extract_status),
    )


def _build_tgz(tmp_path: Path, name: str, xml_body: str) -> Path:
    """Build a minimal PMCLiteMetadata-shaped tgz (an ``out/<name>.xml``
    member holding ``<PMCSet>{xml_body}</PMCSet>``) -- same tar shape as the
    real feed (see enrich_from_lite's module docstring), just synthetic
    content so a single test can exercise a specific field combination."""
    xml = f"<PMCSet>{xml_body}</PMCSet>".encode()
    tgz_path = tmp_path / name
    with tarfile.open(tgz_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="out/PMC.1.xml")
        info.size = len(xml)
        tf.addfile(info, io.BytesIO(xml))
    return tgz_path


def test_enrich_from_lite_fills_matched_rows_and_skips_others(pg_conn):
    _setup_schema(pg_conn)

    from episteme.data.europepmc.lite_metadata.enrich_from_lite import enrich_from_lite

    with pg_conn.cursor() as cur:
        # 2 rows with pmcid set, journal/year NULL -- matched in sample.tgz
        # (PMC17774: Arthritis research/1999; PMC7441394: Scientific reports/2020).
        # PMC17774 keeps a NULL year (defensive case -- NULL should still get
        # filled). PMC7441394 seeds year=0 -- the REAL sentinel value
        # postgres_loader._coerce_year always writes for an unknown year
        # (never NULL); this is the case that actually matters (regression
        # coverage for the CASE-based fill; coalesce() alone can never fire
        # on 0, only on NULL).
        _seed_article(cur, id_="pmcid:PMC17774", pmcid="PMC17774")
        _seed_article(cur, id_="pmcid:PMC7441394", pmcid="PMC7441394", year=0)
        # journal ALREADY set (non-NULL) -- coalesce must NOT overwrite it;
        # year is NULL and IS matched in the fixture (PMC7445587: 2020).
        _seed_article(
            cur,
            id_="pmcid:PMC7445587",
            pmcid="PMC7445587",
            journal="Pre-existing Journal",
        )
        # no matching pmcid anywhere in sample.tgz -- must be completely untouched.
        _seed_article(cur, id_="pmcid:PMCNOMATCH", pmcid="PMCNOMATCH999")
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT id, extract_status FROM episteme.articles ORDER BY id")
        before = dict(cur.fetchall())

    res = enrich_from_lite(FIXTURE, pg_conn)
    pg_conn.commit()

    # sample.tgz carries 4 real records: PMC17774, PMC7441394, PMC7445587,
    # PMC29327 -- the last one has no matching seeded row (proves a
    # non-matching feed record is a no-op, not an error).
    assert res["records_read"] == 4
    assert res["rows_matched"] == 3
    assert res["rows_updated"] == 3

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT journal, year, mesh FROM episteme.articles WHERE pmcid = %s",
            ("PMC17774",),
        )
        assert cur.fetchone() == ("Arthritis research", 1999, None)

        cur.execute(
            "SELECT journal, year, mesh FROM episteme.articles WHERE pmcid = %s",
            ("PMC7441394",),
        )
        assert cur.fetchone() == ("Scientific reports", 2020, None)

        # journal untouched (coalesce), year filled from the fixture.
        cur.execute(
            "SELECT journal, year FROM episteme.articles WHERE pmcid = %s",
            ("PMC7445587",),
        )
        assert cur.fetchone() == ("Pre-existing Journal", 2020)

        # no match in the feed -> completely untouched.
        cur.execute(
            "SELECT journal, year FROM episteme.articles WHERE pmcid = %s",
            ("PMCNOMATCH999",),
        )
        assert cur.fetchone() == (None, None)

        cur.execute("SELECT id, extract_status FROM episteme.articles ORDER BY id")
        after = dict(cur.fetchall())
        assert after == before  # extract_status is never touched by this pass.


def test_enrich_from_lite_fills_mesh_when_present_in_feed(pg_conn, tmp_path):
    """The real PMCLiteMetadata feed never carries MeSH data (see
    enrich_from_lite's module docstring -- confirmed against a real ~41.7k
    record slice), so this exercises the mandated `mesh` UPDATE clause
    against a SYNTHETIC record built with a `meshHeadingList` block, not
    sample.tgz (which mirrors the real, mesh-less shape as committed)."""
    _setup_schema(pg_conn)

    from episteme.data.europepmc.lite_metadata.enrich_from_lite import enrich_from_lite

    with pg_conn.cursor() as cur:
        _seed_article(cur, id_="pmcid:PMCMESH1", pmcid="PMCMESH1")
    pg_conn.commit()

    synthetic = _build_tgz(
        tmp_path,
        "synthetic_mesh.tgz",
        "<PMC_ARTICLE><pmcid>PMCMESH1</pmcid><JournalTitle>Synthetic Journal</JournalTitle>"
        "<PubYear>2021</PubYear>"
        "<meshHeadingList><meshHeading><descriptorName>Humans</descriptorName></meshHeading>"
        "<meshHeading><descriptorName>Animals</descriptorName></meshHeading></meshHeadingList>"
        "</PMC_ARTICLE>",
    )

    res = enrich_from_lite(synthetic, pg_conn)
    pg_conn.commit()

    assert res == {"records_read": 1, "rows_matched": 1, "rows_updated": 1}

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT journal, year, mesh FROM episteme.articles WHERE pmcid = %s",
            ("PMCMESH1",),
        )
        row = cur.fetchone()
        assert row[0] == "Synthetic Journal"
        assert row[1] == 2021
        assert sorted(row[2]) == ["Animals", "Humans"]


def test_enrich_from_lite_does_not_overwrite_existing_mesh(pg_conn, tmp_path):
    _setup_schema(pg_conn)

    from episteme.data.europepmc.lite_metadata.enrich_from_lite import enrich_from_lite

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episteme.articles (id, source, source_file, pmcid, mesh, extract_status)
            VALUES ('pmcid:PMCMESH2', 'pubmed', 'seed.xml', 'PMCMESH2', %s, 'ok')
            """,
            (["Existing Term"],),
        )
    pg_conn.commit()

    synthetic = _build_tgz(
        tmp_path,
        "synthetic_mesh2.tgz",
        "<PMC_ARTICLE><pmcid>PMCMESH2</pmcid>"
        "<meshHeadingList><meshHeading><descriptorName>New Term</descriptorName></meshHeading>"
        "</meshHeadingList></PMC_ARTICLE>",
    )

    enrich_from_lite(synthetic, pg_conn)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT mesh, extract_status FROM episteme.articles WHERE pmcid = %s",
            ("PMCMESH2",),
        )
        row = cur.fetchone()
        assert row[0] == ["Existing Term"]
        assert row[1] == "ok"


def test_enrich_from_lite_moves_row_across_year_partitions_for_pmc_source(pg_conn):
    """Fix 1 follow-up: episteme.articles_pmc (and articles_bookshelf) are
    RANGE-sub-partitioned by year, unlike the unpartitioned
    articles_default the other seeded tests above land in (source='pubmed').
    Seed a source='pmc' row at year=0 (articles_pmc_y0, the real sentinel
    postgres_loader writes) and confirm the CASE-based UPDATE both fills the
    real year (1999, from sample.tgz's PMC17774) AND physically moves the
    row into episteme.articles_pmc_1995_2000 -- the cross-partition row
    move the brief reasoned about analytically but told the fix wave not to
    re-verify functionally. Postgres does support UPDATE-driven partition
    row movement (since PG11); this exercises it for real."""
    _setup_schema(pg_conn)

    from episteme.data.europepmc.lite_metadata.enrich_from_lite import enrich_from_lite

    with pg_conn.cursor() as cur:
        _seed_article(cur, id_="pmcid:PMC17774-pmc", pmcid="PMC17774", year=0, source="pmc")
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT year, tableoid::regclass::text FROM episteme.articles WHERE id = %s",
            ("pmcid:PMC17774-pmc",),
        )
        before_year, before_partition = cur.fetchone()
        assert before_year == 0
        assert before_partition == "episteme.articles_pmc_y0"

    enrich_from_lite(FIXTURE, pg_conn)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT year, tableoid::regclass::text FROM episteme.articles WHERE id = %s",
            ("pmcid:PMC17774-pmc",),
        )
        after_year, after_partition = cur.fetchone()
        assert after_year == 1999
        assert after_partition == "episteme.articles_pmc_1995_2000"
