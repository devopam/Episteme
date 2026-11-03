"""reg_record loader: release partitions, validation, latest switch.

pg-marked: runs against ``episteme_test`` only. The schema is built once per
module (a rebuild costs minutes on the test disk); each test clears the reg tables.
"""

import contextlib
import json
import os
import zipfile
from pathlib import Path

import ijson
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.data.regulatory import load_reg
from episteme.data.regulatory.feeds import Feed

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/episteme/data/db/migrations"
MIGRATION_0002 = MIGRATIONS / "0002_container_and_book_parts.sql"
MIGRATION_0010 = MIGRATIONS / "0010_regulatory_sources.sql"


def _build_schema(conn):
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((REPO_ROOT / "src/episteme/data/db/extensions.sql").read_text("utf-8"))
        cur.execute((REPO_ROOT / "src/episteme/data/db/schema.sql").read_text("utf-8"))
        cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0010.read_text(encoding="utf-8"))
    conn.commit()


def _clear_reg(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'episteme.reg_record'::regclass"
        )
        for (name,) in cur.fetchall():
            cur.execute(f'DROP TABLE episteme."{name}" CASCADE')
        cur.execute("DELETE FROM episteme.reg_release")
    conn.commit()


@pytest.fixture(scope="module")
def _schema():
    """Build the schema once for this module (a rebuild costs minutes on the test
    disk). Its own connection: ``pg_conn`` is function-scoped. Same guard as
    ``pg_conn``: only ``episteme_test``."""
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    if conninfo_to_dict(dsn).get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to DROP SCHEMA")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        _build_schema(conn)
    finally:
        conn.close()


@pytest.fixture
def db(_schema, pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "processed"))
    from episteme.config import get_settings

    get_settings.cache_clear()
    _clear_reg(pg_conn)
    yield pg_conn
    pg_conn.rollback()
    get_settings.cache_clear()


def _feed(**kw) -> Feed:
    base = dict(
        name="ema_test",
        url_key="K",
        format="ema_json",
        jurisdiction="EU",
        record_key=("url",),
        substance_fields=("sub",),
        product_fields=("prod",),
        date_field="date",
        cadence="monthly",
    )
    base.update(kw)
    return Feed(**base)


def _ema_file(path: Path, rows: list[dict], total: int | None = None) -> Path:
    meta = {"total_records": len(rows) if total is None else total}
    path.write_text(json.dumps({"meta": meta, "data": rows}), encoding="utf-8")
    return path


def _rows(n: int, start: int = 0) -> list[dict]:
    return [
        {
            "url": f"u{i}",
            "sub": f"Drug {i} | Salt{i}",
            "prod": f"EMEA/H/C/{i}",
            "date": "2025-01-02",
        }
        for i in range(start, start + n)
    ]


def _count(conn, sql_text: str, *params) -> int:
    with conn.cursor() as cur:
        cur.execute(sql_text, params)
        return cur.fetchone()[0]


def test_release_becomes_latest_only_after_validation(db, tmp_path, monkeypatch):
    feed = _feed()
    seen = []
    real = load_reg._build_indexes

    def spy(cur, leaf):
        # at index time the new release must not be latest yet
        seen.append(load_reg.latest_release(db, feed.name))
        real(cur, leaf)

    monkeypatch.setattr(load_reg, "_build_indexes", spy)
    f = _ema_file(tmp_path / "a.json", _rows(3))
    assert load_reg.latest_release(db, feed.name) is None
    res = load_reg.load_release(db, feed, f, "2026-11", run_id="r1")
    db.commit()
    assert seen == [None]
    assert res["status"] == "loaded" and res["rows"] == 3 and res["duplicates"] == 0
    assert load_reg.latest_release(db, feed.name) == "2026-11"
    _, leaf = load_reg.partition_names(feed.name, "2026-11")
    assert _count(db, f"SELECT count(*) FROM episteme.{leaf}") == 3
    # normalised columns and the four indexes
    with db.cursor() as cur:
        cur.execute(
            f"SELECT substance_names, product_keys, event_date::text FROM episteme.{leaf} "
            "WHERE record_key = 'u1'"
        )
        assert cur.fetchone() == (["drug 1", "salt1"], ["EMEA/H/C/1"], "2025-01-02")
        cur.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname='episteme' AND tablename=%s", (leaf,)
        )
        assert cur.fetchone()[0] == 4


def test_short_feed_does_not_become_latest(db, tmp_path):
    feed = _feed()
    ok = _ema_file(tmp_path / "ok.json", _rows(2))
    load_reg.load_release(db, feed, ok, "2026-10", run_id="r1")
    db.commit()
    short = _ema_file(tmp_path / "short.json", _rows(3), total=5)
    res = load_reg.load_release(db, feed, short, "2026-11", run_id="r2")
    db.commit()
    assert res["status"] == "failed" and "meta.total_records" in res["reason"]
    assert load_reg.latest_release(db, feed.name) == "2026-10"
    _, leaf = load_reg.partition_names(feed.name, "2026-11")
    assert _count(db, "SELECT count(*) FROM pg_class WHERE relname = %s", leaf) == 0
    assert (
        _count(
            db,
            "SELECT count(*) FROM episteme.reg_release WHERE release_id='2026-11' "
            "AND status='failed' AND NOT is_latest",
        )
        == 1
    )


def test_truncated_file_raises_and_leaves_nothing(db, tmp_path):
    feed = _feed()
    bad = tmp_path / "cut.json"
    bad.write_text(
        '{"meta": {"total_records": 3}, "data": [{"url": "u1"}, {"url"', encoding="utf-8"
    )
    with pytest.raises(ijson.JSONError):
        load_reg.load_release(db, feed, bad, "2026-11", run_id="r1")
    db.rollback()
    _, leaf = load_reg.partition_names(feed.name, "2026-11")
    assert _count(db, "SELECT count(*) FROM pg_class WHERE relname = %s", leaf) == 0
    assert load_reg.latest_release(db, feed.name) is None


def test_new_release_keeps_the_old_partition(db, tmp_path):
    feed = _feed()
    load_reg.load_release(db, feed, _ema_file(tmp_path / "1.json", _rows(2)), "2026-10", run_id="r")
    load_reg.load_release(db, feed, _ema_file(tmp_path / "2.json", _rows(4)), "2026-11", run_id="r")
    db.commit()
    assert load_reg.latest_release(db, feed.name) == "2026-11"
    assert _count(db, "SELECT count(*) FROM episteme.reg_release WHERE is_latest") == 1
    assert (
        _count(
            db,
            "SELECT count(*) FROM episteme.reg_record "
            "WHERE dataset = 'ema_test' AND release_id = '2026-10'",
        )
        == 2
    )
    assert (
        _count(
            db,
            "SELECT count(*) FROM episteme.reg_record "
            "WHERE dataset = 'ema_test' AND release_id = '2026-11'",
        )
        == 4
    )


def test_reload_of_same_release_is_idempotent(db, tmp_path):
    feed = _feed()
    f = _ema_file(tmp_path / "a.json", _rows(3))
    first = load_reg.load_release(db, feed, f, "2026-11", run_id="r1")
    db.commit()
    second = load_reg.load_release(db, feed, f, "2026-11", run_id="r2")
    db.commit()
    assert first["rows"] == second["rows"] == 3 and second["status"] == "loaded"
    assert _count(db, "SELECT count(*) FROM episteme.reg_release") == 1
    assert _count(db, "SELECT count(*) FROM episteme.reg_record") == 3
    assert load_reg.latest_release(db, feed.name) == "2026-11"
    # a failed reload leaves the loaded release as it was
    short = _ema_file(tmp_path / "short.json", _rows(2), total=9)
    res = load_reg.load_release(db, feed, short, "2026-11", run_id="r3")
    db.commit()
    assert res["status"] == "failed"
    assert _count(db, "SELECT count(*) FROM episteme.reg_record") == 3
    assert load_reg.latest_release(db, feed.name) == "2026-11"
    assert _count(db, "SELECT rows FROM episteme.reg_release") == 3


def test_exact_duplicates_are_dropped_before_the_total_check(db, tmp_path):
    feed = _feed()
    rows = _rows(3) + [_rows(3)[0], _rows(3)[1]]
    f = _ema_file(tmp_path / "herbal.json", rows, total=3)
    res = load_reg.load_release(db, feed, f, "2026-11", run_id="r")
    db.commit()
    assert res["status"] == "loaded" and res["rows"] == 3 and res["duplicates"] == 2


def test_non_exact_repeat_keeps_the_last_row_and_is_counted(db, tmp_path):
    feed = _feed()
    rows = [{"url": "u1", "x": 1}, {"url": "u2", "x": 1}, {"url": "u1", "x": 2}]
    f = _ema_file(tmp_path / "a.json", rows, total=3)
    res = load_reg.load_release(db, feed, f, "2026-11", run_id="r")
    db.commit()
    assert res["status"] == "loaded" and res["rows"] == 2 and res["conflicts"] == 1
    assert (
        _count(
            db,
            "SELECT (payload->>'x')::int FROM episteme.reg_record "
            "WHERE dataset='ema_test' AND release_id='2026-11' AND record_key='u1'",
        )
        == 2
    )


def test_odd_release_id_cannot_break_or_inject(db, tmp_path):
    feed = _feed()
    evil = 'x"; DROP TABLE episteme.articles; --'
    f = _ema_file(tmp_path / "a.json", _rows(1))
    res = load_reg.load_release(db, feed, f, evil, run_id="r")
    db.commit()
    assert res["status"] == "loaded"
    assert _count(db, "SELECT count(*) FROM pg_class WHERE relname = 'articles'") == 1
    assert load_reg.latest_release(db, feed.name) == evil


def test_openfda_zips_load_with_input_key_source_file(db, tmp_path):
    raw = tmp_path / "raw" / "openfda"
    paths = []
    for part in ("a", "b"):
        d = raw / "drug" / "enforcement" / part
        d.mkdir(parents=True)
        z = d / "drug-enforcement-0001.json.zip"
        with zipfile.ZipFile(z, "w") as zf:
            body = {
                "results": [{"recall_number": f"{part}-1", "openfda": {"substance_name": ["X"]}}]
            }
            zf.writestr("drug-enforcement-0001.json", json.dumps(body))
        paths.append(z)
    feed = _feed(
        name="fda_enforcement",
        format="openfda_json",
        jurisdiction="US",
        record_key=("recall_number",),
        substance_fields=("openfda.substance_name",),
        product_fields=(),
        date_field=None,
    )
    res = load_reg.load_release(db, feed, paths, "2026-11", run_id="r", raw_dir=raw)
    db.commit()
    assert res["status"] == "loaded" and res["rows"] == 2
    with db.cursor() as cur:
        cur.execute(
            "SELECT record_key, source_file, substance_names FROM episteme.reg_record "
            "WHERE dataset='fda_enforcement' AND release_id='2026-11' ORDER BY record_key"
        )
        assert cur.fetchall() == [
            ("a-1", "drug__enforcement__a__drug-enforcement-0001.json.zip", ["x"]),
            ("b-1", "drug__enforcement__b__drug-enforcement-0001.json.zip", ["x"]),
        ]


def test_latest_release_prunes_to_one_partition(db, tmp_path):
    feed = _feed()
    load_reg.load_release(db, feed, _ema_file(tmp_path / "1.json", _rows(2)), "2026-10", run_id="r")
    load_reg.load_release(db, feed, _ema_file(tmp_path / "2.json", _rows(2)), "2026-11", run_id="r")
    db.commit()
    with db.cursor() as cur:
        cur.execute(
            "EXPLAIN SELECT * FROM episteme.reg_record "
            "WHERE dataset = 'ema_test' AND release_id = '2026-11'"
        )
        plan = "\n".join(r[0] for r in cur.fetchall())
    assert plan.count("reg_record_ema_test") == 1


def test_main_runs_feeds_end_to_end_and_exits_non_zero_for_a_short_feed(db, tmp_path, monkeypatch):
    import episteme.ops.stop as stop
    from episteme.data.db import connection as db_connection

    raw = tmp_path / "raw"
    raw.mkdir()
    _ema_file(raw / "ema_orphan.json", [{"orphan_designation_url": f"u{i}"} for i in range(3)])
    _ema_file(raw / "ema_pip.json", [{"pip_url": "u1"}], total=5)  # short: must fail
    monkeypatch.setattr(db_connection, "admin_dsn_from_settings", lambda: os.environ["TEST_PG_DSN"])
    monkeypatch.setattr(
        stop,
        "stage_guard",
        lambda *a, **k: contextlib.nullcontext(type("H", (), {"exit_code": lambda s, rc: rc})()),
    )
    argv = ["--source", "ema", "--raw-dir", str(raw), "--only", "ema_orphan", "--only", "ema_pip"]
    assert load_reg.main(argv) == 1
    db.rollback()
    assert load_reg.latest_release(db, "ema_orphan") is not None
    assert load_reg.latest_release(db, "ema_pip") is None
    assert _count(db, "SELECT count(*) FROM episteme.reg_record WHERE dataset = 'ema_orphan'") == 3
    db.rollback()  # an idle-in-transaction read would block the DDL of the next main
    # resume: the loaded feed is skipped, the short feed fails again
    assert load_reg.main(argv) == 1
    db.rollback()
    assert _count(db, "SELECT count(*) FROM episteme.reg_release WHERE dataset = 'ema_orphan'") == 1
    db.rollback()


def test_open_read_transaction_makes_the_feed_fail_not_hang(db, tmp_path, monkeypatch):
    from episteme.config import get_settings

    monkeypatch.setenv("EPISTEME_REG_LOCK_TIMEOUT_S", "1")
    get_settings.cache_clear()
    feed = _feed()
    f = _ema_file(tmp_path / "a.json", _rows(2))
    reader = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=False)
    try:
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM episteme.reg_record")  # holds the lock
        res = load_reg.load_release(db, feed, f, "2026-11", run_id="r")
        db.commit()
        assert res["status"] == "failed" and "reg_record" in res["reason"]
        assert load_reg.latest_release(db, feed.name) is None
        _, leaf = load_reg.partition_names(feed.name, "2026-11")
        assert _count(db, "SELECT count(*) FROM pg_class WHERE relname = %s", leaf) == 0
    finally:
        reader.rollback()
        reader.close()
    # the reader is gone: the same feed now loads
    res = load_reg.load_release(db, feed, f, "2026-11", run_id="r")
    db.commit()
    assert res["status"] == "loaded"


def _run_feeds(conn, feeds, raw, processed):
    out = load_reg.run_feeds(
        conn, "ema", feeds, raw, processed, release=None, force=False, reason=None, run_id="t"
    )
    conn.rollback()
    return out


def test_a_marker_with_no_release_in_the_database_loads_again(db, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    feed = _feed()
    _ema_file(raw / "ema_test.json", _rows(2))
    processed = tmp_path / "processed"
    assert _run_feeds(db, [feed], raw, processed)["loaded"] == 1
    assert _run_feeds(db, [feed], raw, processed)["skipped"] == 1
    (marker,) = processed.rglob("*.ok")
    assert marker.name.startswith("episteme_test_")  # the database name is in the key
    _clear_reg(db)  # the rows go; the marker stays
    assert _run_feeds(db, [feed], raw, processed)["loaded"] == 1
    assert load_reg.latest_release(db, feed.name) is not None
    db.rollback()


def test_an_ema_release_resolves_its_names_in_its_own_transaction(db, tmp_path):
    with db.cursor() as cur:
        cur.execute("TRUNCATE episteme.substance, episteme.substance_xref")
        cur.execute("INSERT INTO episteme.substance (unii) VALUES ('U1') RETURNING substance_key")
        key = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO episteme.substance_xref "
            "(substance_key, id_type, id_value, method, confidence, source) "
            "VALUES (%s, 'name', 'metformin', 'normalised_name', 1.0, 'fda_substance')",
            (key,),
        )
    db.commit()
    raw = tmp_path / "raw"
    raw.mkdir()
    rows = [{"url": "u1", "sub": "Metformin | Unknownium", "prod": "P", "date": "2025-01-02"}]
    _ema_file(raw / "ema_test.json", rows)
    processed = tmp_path / "processed"
    assert _run_feeds(db, [_feed()], raw, processed)["loaded"] == 1
    with db.cursor() as cur:
        cur.execute(
            "SELECT substance_key, id_value, source FROM episteme.substance_xref "
            "WHERE id_type = 'ema_name'"
        )
        assert cur.fetchall() == [(key, "metformin", "ema")]
        cur.execute(
            "SELECT new_value FROM episteme._audit "
            "WHERE object LIKE 'substance_xref ema_name%%' ORDER BY seq DESC LIMIT 1"
        )
        (counts,) = cur.fetchone()
    db.rollback()
    assert counts["matched"] == 1 and counts["unmatched"] == 1
    stats = json.loads(next(processed.rglob("*.ok")).read_text("utf-8"))["stats"]
    assert stats["names"]["matched"] == 1


def test_a_marker_made_for_episteme_test_is_not_used_for_episteme(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    feed = _feed()
    _ema_file(raw / "ema_test.json", _rows(2))
    processed = tmp_path / "processed"
    assert _run_feeds(db, [feed], raw, processed)["loaded"] == 1  # the real episteme_test
    # The same files, the same rows, another database name: its marker is missing.
    monkeypatch.setattr(load_reg, "database_name", lambda conn: "episteme")
    assert _run_feeds(db, [feed], raw, processed)["loaded"] == 1
    assert _run_feeds(db, [feed], raw, processed)["skipped"] == 1
    assert len(list(processed.rglob("*.ok"))) == 2
