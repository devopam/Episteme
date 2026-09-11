import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0002 = REPO_ROOT / "src/episteme/data/db/migrations/0002_container_and_book_parts.sql"
FX = REPO_ROOT / "tests" / "fixtures" / "sp2" / "bookshelf"


def _setup_schema(conn):
    with (
        conn.cursor() as cur,
        open("src/episteme/data/db/extensions.sql") as ext,
        open("src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    conn.commit()


def _configure(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()


def test_bookshelf_load_and_graph_end_to_end(pg_conn, tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    _setup_schema(pg_conn)
    # schema.sql carries container_id/book_meta but NOT episteme.article_parts /
    # the bookshelf partitions (Task 2 put that DDL in migration 0002 only) --
    # apply 0002 after the schema rebuild. It is idempotent.
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
    pg_conn.commit()

    from episteme.data import graph_builder, postgres_loader
    from episteme.data.bookshelf.extract_bookshelf import extract_bookshelf

    res = extract_bookshelf(FX, tmp_path)
    assert res["ok"] == 1
    assert res["rows"] == 3

    shard = sorted((tmp_path / "staging" / "bookshelf").glob("*.*"))[0]

    load_res = postgres_loader.load_source_file(
        pg_conn, source="bookshelf", staging_path=shard, run_id="t"
    )
    pg_conn.commit()
    assert load_res["rows"] == 3

    graph_res = graph_builder.build(pg_conn, source="bookshelf", raw_dir=tmp_path, run_id="t")
    pg_conn.commit()
    assert graph_res["parts"] == 2

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source_file = %s", ("NBK1.tar.gz",)
        )
        assert cur.fetchone()[0] == 3

        cur.execute("SELECT count(*) FROM episteme.article_parts")
        assert cur.fetchone()[0] == 2

    assert set(graph_builder.neighbours(pg_conn, "bookshelf:NBK1", kind="part")) == {
        "bookshelf:NBK1:p1",
        "bookshelf:NBK1:p2",
    }

    # CRITICAL proof: book_meta round-trips into a REAL jsonb column, not text.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT jsonb_typeof(book_meta), book_meta->>'isbn' "
            "FROM episteme.articles WHERE id = %s",
            ("bookshelf:NBK1",),
        )
        typeof, isbn = cur.fetchone()
        assert typeof == "object"
        assert isbn == "978-0-000-00000-0"

        cur.execute(
            "SELECT container_id FROM episteme.articles WHERE id = %s", ("bookshelf:NBK1:p1",)
        )
        assert cur.fetchone()[0] == "bookshelf:NBK1"
