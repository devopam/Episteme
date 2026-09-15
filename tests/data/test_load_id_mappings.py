"""``pg`` tests for the Europe PMC id_mappings -> episteme.id_map loader.

``episteme.id_map`` (+ its coalesced unique index ``id_map_ids_uq``) is already
in ``db/schema.sql`` (not a migration), so the plain ``_setup_schema`` rebuild
below gets it for free -- no extra migration-apply step, unlike article_parts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "sp2"
    / "id_mappings"
    / "PMID_PMCID_DOI.csv.gz"
)


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


def test_load_id_mappings_inserts_rows(pg_conn):
    _setup_schema(pg_conn)

    from episteme.data.europepmc.id_mappings.load_id_mappings import load_id_mappings

    res = load_id_mappings(FIXTURE, pg_conn)
    pg_conn.commit()

    assert res["rows_read"] == 5
    assert res["rows_inserted"] == 5
    assert res["rows_skipped_dupe"] == 0

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.id_map")
        assert cur.fetchone()[0] == 5

        cur.execute(
            "SELECT pmid, pmcid, doi FROM episteme.id_map WHERE pmcid = %s",
            ("PMC1234567",),
        )
        row = cur.fetchone()
        assert row == ("12345678", "PMC1234567", "https://doi.org/10.1000/testdoi1")

        # empty CSV fields normalize to NULL, not the literal empty string.
        cur.execute(
            "SELECT pmid, doi FROM episteme.id_map WHERE pmcid = %s",
            ("PMC10000002",),
        )
        row = cur.fetchone()
        assert row == (None, None)

        cur.execute("SELECT source_file FROM episteme.id_map LIMIT 1")
        assert cur.fetchone()[0] == "PMID_PMCID_DOI.csv.gz"


def test_load_id_mappings_rerun_is_idempotent(pg_conn):
    _setup_schema(pg_conn)

    from episteme.data.europepmc.id_mappings.load_id_mappings import load_id_mappings

    res1 = load_id_mappings(FIXTURE, pg_conn)
    pg_conn.commit()
    assert res1["rows_inserted"] == 5

    res2 = load_id_mappings(FIXTURE, pg_conn)
    pg_conn.commit()

    assert res2["rows_read"] == 5
    assert res2["rows_inserted"] == 0
    assert res2["rows_skipped_dupe"] == 5

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.id_map")
        assert cur.fetchone()[0] == 5


def test_load_id_mappings_corrupt_gzip_raises_or_errors_cleanly(pg_conn, tmp_path):
    _setup_schema(pg_conn)

    from episteme.data.europepmc.id_mappings.load_id_mappings import load_id_mappings

    bad = tmp_path / "truncated.csv.gz"
    # A real gzip header/stream, truncated mid-member -> not a valid gzip file.
    good_bytes = FIXTURE.read_bytes()
    bad.write_bytes(good_bytes[: len(good_bytes) // 2])

    res = load_id_mappings(bad, pg_conn)

    assert "error" in res

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.id_map")
        assert cur.fetchone()[0] == 0
