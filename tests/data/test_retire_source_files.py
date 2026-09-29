"""pg-marked tests for postgres_loader.retire_source_files (SP7 Task 4).

Follows tests/data/test_postgres_loader.py's / tests/data/test_audit_trail.py's
pg_conn + _setup_schema pattern: DROP SCHEMA + reapply extensions.sql +
schema.sql on the fixture connection, then explicit INSERTs and
SELECT count(*) assertions (no ORM, no fixtures beyond raw SQL).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]

# I3 (whole-branch review): obviously fake release names, so a test row can
# never be mistaken for -- or collide with -- a real quarterly release.
OLD_SF = "SDTM__1999-01-01__SDTM_Terminology.txt"
NEW_SF = "SDTM__1999-04-01__SDTM_Terminology.txt"


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


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    importlib.reload(audit_trail)


def _insert_article(conn, *, id_, source, source_file):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file) VALUES (%s, %s, %s)",
            (id_, source, source_file),
        )


def _insert_body(conn, *, article_id, source, text="body text"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, text) VALUES (%s, %s, %s)",
            (article_id, source, text),
        )


def test_retires_old_release_keeps_new_and_other_sources(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C2:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C2:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    _insert_article(pg_conn, id_="mesh:D000001", source="mesh", source_file="desc2026.xml")
    pg_conn.commit()

    deleted = postgres_loader.retire_source_files(
        pg_conn, source="cdisc_ct", keep_source_files=[NEW_SF], run_id="r1"
    )
    assert deleted == 2

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct' AND source_file=%s",
            (OLD_SF,),
        )
        assert cur.fetchone()[0] == 0

        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct' AND source_file=%s",
            (NEW_SF,),
        )
        assert cur.fetchone()[0] == 2

        cur.execute("SELECT count(*) FROM episteme.articles WHERE source='mesh'")
        assert cur.fetchone()[0] == 1


def test_article_body_follows(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C2:p1", source="cdisc_ct", source_file=NEW_SF)
    # a mesh row reusing the SAME article_id as a retired cdisc_ct row, to prove
    # the article_body delete is scoped by source (never touches another source).
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="mesh", source_file="desc2026.xml")
    pg_conn.commit()

    _insert_body(pg_conn, article_id="cdisc_ct:SDTM:C1:p1", source="cdisc_ct")
    _insert_body(pg_conn, article_id="cdisc_ct:SDTM:C2:p1", source="cdisc_ct")
    _insert_body(pg_conn, article_id="cdisc_ct:SDTM:C1:p1", source="mesh")
    pg_conn.commit()

    deleted = postgres_loader.retire_source_files(
        pg_conn, source="cdisc_ct", keep_source_files=[NEW_SF], run_id="r1"
    )
    assert deleted == 1

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.article_body "
            "WHERE article_id='cdisc_ct:SDTM:C1:p1' AND source='cdisc_ct'"
        )
        assert cur.fetchone()[0] == 0

        cur.execute(
            "SELECT count(*) FROM episteme.article_body "
            "WHERE article_id='cdisc_ct:SDTM:C2:p1' AND source='cdisc_ct'"
        )
        assert cur.fetchone()[0] == 1

        # the mesh row sharing the retired article_id must survive untouched.
        cur.execute(
            "SELECT count(*) FROM episteme.article_body "
            "WHERE article_id='cdisc_ct:SDTM:C1:p1' AND source='mesh'"
        )
        assert cur.fetchone()[0] == 1


def test_records_load_replace_audit(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    pg_conn.commit()

    deleted = postgres_loader.retire_source_files(
        pg_conn, source="cdisc_ct", keep_source_files=[NEW_SF], run_id="r1"
    )
    assert deleted == 1

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT object, rows_affected, run_id FROM episteme._audit "
            "WHERE event_type='load_replace' ORDER BY seq DESC LIMIT 1"
        )
        object_, rows_affected, run_id = cur.fetchone()
        assert object_ == "cdisc_ct retire"
        assert rows_affected == 1
        assert run_id == "r1"


SEND_OLD_SF = "SEND__1999-01-01__SEND_Terminology.txt"
SEND_NEW_SF = "SEND__1999-04-01__SEND_Terminology.txt"


def _count(conn, source_file):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct' AND source_file=%s",
            (source_file,),
        )
        return cur.fetchone()[0]


def test_scope_prefix_leaves_other_packages_alone(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SEND:C1:p1", source="cdisc_ct", source_file=SEND_OLD_SF)
    pg_conn.commit()

    deleted = postgres_loader.retire_source_files(
        pg_conn, source="cdisc_ct", keep_source_files=[NEW_SF], run_id="r1", scope_prefix="SDTM__"
    )
    assert deleted == 1
    assert _count(pg_conn, OLD_SF) == 0
    assert _count(pg_conn, NEW_SF) == 1
    assert _count(pg_conn, SEND_OLD_SF) == 1  # another package: out of scope, untouched


def _raw_release(raw, package, date):
    d = raw / package / date
    d.mkdir(parents=True)
    f = d / f"{package}_Terminology.txt"
    f.write_text("placeholder\n", encoding="utf-8")
    return f


def _mark_loaded(processed, source_file):
    from episteme.data import checkpoint_markers

    m = checkpoint_markers.load_success_marker_path(processed, "cdisc_ct", f"{source_file}.parquet")
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("{}", encoding="utf-8")


def _run_retire_cli(monkeypatch, raw, processed):
    import episteme.data.db.connection as conn_mod
    from episteme.data.cdisc_ct import retire

    # connection.py may hold a get_settings object from before another test's
    # importlib.reload(episteme.config); clear the one it actually calls (see
    # tests/test_db_guard.py::test_read_only_mode_blocks_writes).
    conn_mod.get_settings.cache_clear()
    monkeypatch.setattr(conn_mod, "_POOL", None)
    try:
        return retire.main(["--raw-dir", str(raw), "--processed-dir", str(processed)])
    finally:
        if conn_mod._POOL is not None:
            conn_mod._POOL.close()
        conn_mod.get_settings.cache_clear()


def test_cli_never_touches_package_absent_locally(pg_conn, monkeypatch, tmp_path):
    # Regression for SP7 Task 4 review round 1: only SDTM is present in the
    # local raw tree (e.g. a --max-files 1 download); SEND rows loaded earlier
    # must survive the retire run.
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    raw, processed = tmp_path / "raw", tmp_path / "processed"
    _raw_release(raw, "SDTM", "1999-04-01")
    _mark_loaded(processed, NEW_SF)

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SEND:C1:p1", source="cdisc_ct", source_file=SEND_NEW_SF)
    pg_conn.commit()

    assert _run_retire_cli(monkeypatch, raw, processed) == 0
    pg_conn.rollback()  # see rows committed by the CLI's own connection
    assert _count(pg_conn, OLD_SF) == 0
    assert _count(pg_conn, NEW_SF) == 1
    assert _count(pg_conn, SEND_NEW_SF) == 1


def test_cli_skips_unloaded_package_but_retires_loaded_one(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    raw, processed = tmp_path / "raw", tmp_path / "processed"
    _raw_release(raw, "SDTM", "1999-04-01")
    _raw_release(raw, "SEND", "1999-04-01")
    _mark_loaded(processed, NEW_SF)  # SDTM loaded; SEND's new release not loaded yet

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SEND:C1:p1", source="cdisc_ct", source_file=SEND_OLD_SF)
    pg_conn.commit()

    assert _run_retire_cli(monkeypatch, raw, processed) == 0
    pg_conn.rollback()
    assert _count(pg_conn, OLD_SF) == 0  # SDTM old release retired
    assert _count(pg_conn, NEW_SF) == 1
    assert _count(pg_conn, SEND_OLD_SF) == 1  # SEND old kept until its new release loads


def test_cli_skips_package_when_kept_release_has_zero_db_rows(
    pg_conn, monkeypatch, tmp_path, capsys
):
    # I2 (whole-branch review): the local raw tree + load_success marker say
    # NEW_SF is current and loaded, but the DB holds no rows for it at all --
    # retire must refuse to delete OLD_SF's rows on that mismatch.
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    raw, processed = tmp_path / "raw", tmp_path / "processed"
    _raw_release(raw, "SDTM", "1999-04-01")
    _mark_loaded(processed, NEW_SF)

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    pg_conn.commit()

    assert _run_retire_cli(monkeypatch, raw, processed) == 0
    pg_conn.rollback()
    assert _count(pg_conn, OLD_SF) == 1  # nothing retired: the package was skipped
    assert _count(pg_conn, NEW_SF) == 0
    out = capsys.readouterr().out
    assert "SDTM" in out and "skipped" in out


def test_cli_skips_package_when_db_holds_a_newer_dated_release_than_local(
    pg_conn, monkeypatch, tmp_path, capsys
):
    # I2 (whole-branch review): the DB holds a release for this package dated
    # AFTER the local newest -- e.g. this machine's raw tree is stale relative
    # to another machine's more recent load. Retire must refuse to delete
    # anything for the package rather than trust the local "newest" blindly.
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    raw, processed = tmp_path / "raw", tmp_path / "processed"
    _raw_release(raw, "SDTM", "1999-04-01")  # local newest == NEW_SF
    _mark_loaded(processed, NEW_SF)

    newer_elsewhere = "SDTM__1999-07-01__SDTM_Terminology.txt"  # newer than NEW_SF, not local
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    _insert_article(
        pg_conn, id_="cdisc_ct:SDTM:C1:p1:v3", source="cdisc_ct", source_file=newer_elsewhere
    )
    pg_conn.commit()

    assert _run_retire_cli(monkeypatch, raw, processed) == 0
    pg_conn.rollback()
    # nothing retired for SDTM: the DB holds a release newer than the local newest
    assert _count(pg_conn, OLD_SF) == 1
    assert _count(pg_conn, NEW_SF) == 1
    assert _count(pg_conn, newer_elsewhere) == 1
    out = capsys.readouterr().out
    assert "SDTM" in out and "skipped" in out


def test_empty_keep_set_raises_and_deletes_nothing(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme._audit")
        audit_before = cur.fetchone()[0]

    with pytest.raises(ValueError):
        postgres_loader.retire_source_files(
            pg_conn, source="cdisc_ct", keep_source_files=[], run_id="r1"
        )

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct'")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM episteme._audit")
        assert cur.fetchone()[0] == audit_before
