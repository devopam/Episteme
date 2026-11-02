"""reg_record loader: release partitions, validation, latest switch.

pg-marked: runs against ``episteme_test`` only. The schema is built once per
module (a rebuild costs minutes on the test disk); each test clears the reg tables.
"""

import json
import zipfile
from pathlib import Path

import ijson
import pytest

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


@pytest.fixture
def db(pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "processed"))
    from episteme.config import get_settings

    get_settings.cache_clear()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.reg_release')")
        built = cur.fetchone()[0] is not None
    pg_conn.commit()
    if not built:
        _build_schema(pg_conn)
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
