"""The label tables in the load step.

The unmarked tests are pure (a fake connection records the statements). The pg-marked
tests run against ``episteme_test`` only; the schema is built once per module.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.data import load_articles, postgres_loader
from episteme.data.dailymed import extract_dailymed as dm
from episteme.data.dailymed import labels

from ._dailymed_fixtures import inner_zip, outer_zip, section, set_id, spl_xml

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/episteme/data/db/migrations"
KINDS = ("prescription", "otc")


class _Cursor:
    def __init__(self, log: list[str]) -> None:
        self.log, self.rowcount = log, 0

    def execute(self, sql, params=None):
        self.log.append(" ".join(str(sql).split()))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self) -> None:
        self.log: list[str] = []

    def cursor(self):
        return _Cursor(self.log)


# ---------------------------------------------------------------------------
# pure
# ---------------------------------------------------------------------------
def test_pre_load_runs_only_for_dailymed(monkeypatch):
    calls: list[Path] = []

    def fake(conn, shard):
        calls.append(shard)
        return {"label_sections": 3, "label_substances": 2}

    monkeypatch.setattr(labels, "replace_labels", fake)
    shard = Path("staging/dailymed/dailymed_bab__part00001.parquet")
    assert load_articles._pre_load(object(), "dailymed", shard) == {
        "label_sections": 3,
        "label_substances": 2,
    }
    assert calls == [shard]
    for other in ("pmc", "pubmed", "pubchem", "bookshelf", "cdisc_ct", "ema_epar"):
        assert load_articles._pre_load(object(), other, shard) == {}
    assert calls == [shard]  # no other source reached the label step


def test_shard_discovery_does_not_see_the_sidecar(tmp_path):
    staging = tmp_path / "staging" / "dailymed"
    staging.mkdir(parents=True)
    (staging / "dailymed_bab__part00001.parquet").write_bytes(b"x")
    side = labels.sidecar_path(tmp_path, "dailymed_bab__part00001")
    labels.write_sidecar(tmp_path, "dailymed_bab__part00001", [])
    assert side.is_file() and side.parent != staging
    assert [p.name for p in load_articles._discover_shards(staging)] == [
        "dailymed_bab__part00001.parquet"
    ]
    assert labels.shard_sidecar(staging / "dailymed_bab__part00001.parquet") == side


def test_a_shard_without_a_sidecar_is_refused(tmp_path):
    shard = tmp_path / "staging" / "dailymed" / "dailymed_bab__part00001.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="sidecar"):
        labels.replace_labels(_Conn(), shard)


def test_retire_deletes_label_rows_for_dailymed_only(monkeypatch):
    monkeypatch.setattr(postgres_loader.audit_trail, "record", lambda *a, **k: None)
    conn = _Conn()
    postgres_loader.delete_source_files(conn, source="dailymed", source_files=["p1"])
    labels_first = [i for i, s in enumerate(conn.log) if "label_" in s]
    articles_delete = next(
        i for i, s in enumerate(conn.log) if s.startswith("DELETE FROM episteme.articles")
    )
    assert len(labels_first) == 2 and max(labels_first) < articles_delete

    other = _Conn()
    postgres_loader.delete_source_files(other, source="pmc", source_files=["p1"])
    assert not any("label_" in s for s in other.log)  # the old behaviour, unchanged
    assert len(other.log) == 2


# ---------------------------------------------------------------------------
# pg (run by the controller, not in the cloud sandbox)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _schema():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    if conninfo_to_dict(dsn).get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to DROP SCHEMA")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
            cur.execute((REPO_ROOT / "src/episteme/data/db/extensions.sql").read_text("utf-8"))
            cur.execute((REPO_ROOT / "src/episteme/data/db/schema.sql").read_text("utf-8"))
            for name in ("0002_container_and_book_parts.sql", "0010_regulatory_sources.sql"):
                cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(_schema, pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "audit_root"))
    from episteme.config import get_settings

    get_settings.cache_clear()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE episteme.label_section, episteme.label_substance")
        cur.execute("DELETE FROM episteme.article_body WHERE source IN ('dailymed', 'pmc')")
        cur.execute("DELETE FROM episteme.articles WHERE source IN ('dailymed', 'pmc')")
    pg_conn.commit()
    yield pg_conn


def _extract(tmp_path: Path, sids: list[str]) -> tuple[Path, Path]:
    raw, out = tmp_path / "raw", tmp_path / "out"
    members = {
        f"prescription/20240101_{s}.zip": inner_zip(
            spl_xml(
                s,
                sections=[
                    section("34067-9", "1 INDICATIONS", [f"Indicated for {s}."]),
                    section("34073-7", "7 INTERACTIONS", ["Avoid warfarin."]),
                ],
            )
        )
        for s in sids
    }
    outer_zip(raw / "dm_spl_release_human_rx_part1.zip", members)
    dm.extract_dailymed(raw, out, release_kinds=KINDS)
    return raw, out


def _load(conn, out: Path, part: str) -> None:
    shard = out / "staging" / "dailymed" / f"{part}.parquet"
    extra = load_articles._pre_load(conn, "dailymed", shard)
    postgres_loader.load_source_file(conn, source="dailymed", staging_path=shard, run_id="t")
    assert extra["label_sections"] >= 0
    conn.commit()


def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM episteme.{table}")
        return cur.fetchone()[0]


@pytest.mark.pg
def test_a_part_loads_its_labels_and_offsets_point_into_the_stored_text(db, tmp_path):
    sids = [set_id("ab", 1), set_id("ab", 2)]
    _, out = _extract(tmp_path, sids)
    _load(db, out, "dailymed_bab__part00001")
    assert _count(db, "label_section") == 4  # two coded sections with text per label
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM episteme.label_section s
              JOIN episteme.article_body b ON b.article_id = s.article_id
             WHERE s.loinc_code = '34073-7'
               AND substr(b.body_text, s.start_offset + 1, s.end_offset - s.start_offset)
                   = '7 INTERACTIONS' || chr(10) || 'Avoid warfarin.'
            """
        )
        assert cur.fetchone()[0] == 2
        cur.execute(
            "SELECT DISTINCT unii FROM episteme.label_substance WHERE unii IS NOT NULL ORDER BY 1"
        )
        assert [r[0] for r in cur.fetchall()] == ["AAAAAAAAAA", "BBBBBBBBBB"]
        cur.execute("SELECT count(*) FROM episteme.label_substance WHERE ndc_product = '123450678'")
        assert cur.fetchone()[0] == 2


@pytest.mark.pg
def test_a_second_load_replaces_the_label_rows_of_the_part(db, tmp_path):
    sids = [set_id("ab", 1), set_id("ab", 2)]
    raw, out = _extract(tmp_path, sids)
    _load(db, out, "dailymed_bab__part00001")
    before = _count(db, "label_section"), _count(db, "label_substance")
    _load(db, out, "dailymed_bab__part00001")  # the same part again: no growth
    assert (_count(db, "label_section"), _count(db, "label_substance")) == before
    # The part is written again with one label less: its rows leave with it.
    outer_zip(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            f"prescription/20240101_{sids[0]}.zip": inner_zip(
                spl_xml(
                    sids[0], sections=[section("34073-7", "7 INTERACTIONS", ["Avoid warfarin."])]
                )
            )
        },
    )
    # One of two known labels leaves: over the withdrawal limit, so the guard is overridden
    # (audited), as an operator would.
    dm.extract_dailymed(raw, out, force=True, force_reason="test", release_kinds=KINDS)
    _load(db, out, "dailymed_bab__part00001")
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT article_id FROM episteme.label_section")
        assert [r[0] for r in cur.fetchall()] == [f"dailymed:{sids[0]}"]
        cur.execute("SELECT DISTINCT article_id FROM episteme.label_substance")
        assert [r[0] for r in cur.fetchall()] == [f"dailymed:{sids[0]}"]


@pytest.mark.pg
def test_retire_removes_the_label_rows_and_nothing_else(db, tmp_path):
    _, out = _extract(tmp_path, [set_id("ab", 1)])
    _load(db, out, "dailymed_bab__part00001")
    with db.cursor() as cur:  # a row of another article must stay
        cur.execute("INSERT INTO episteme.label_section VALUES ('other:1', '34073-7', 't', 0, 5)")
    db.commit()
    postgres_loader.delete_source_files(
        db, source="dailymed", source_files=["dailymed_bab__part00001"], run_id="t"
    )
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT article_id FROM episteme.label_section")
        assert [r[0] for r in cur.fetchall()] == ["other:1"]
    assert _count(db, "label_substance") == 0
    assert _count(db, "label_section") == 1


@pytest.mark.pg
def test_a_label_that_moves_to_another_part_leaves_no_duplicate_label_rows(
    db, tmp_path, monkeypatch
):
    sids = [set_id("ab", n) for n in (1, 2, 3)]
    raw, out = tmp_path / "raw", tmp_path / "out"
    members = {
        f"prescription/20240101_{s}.zip": inner_zip(
            spl_xml(s, sections=[section("34073-7", "7 INTERACTIONS", ["Avoid warfarin."])])
        )
        for s in sids
    }
    outer_zip(raw / "dm_spl_release_human_rx_part1.zip", members)
    monkeypatch.setattr(dm, "_part_caps", lambda: (2, 10**12))
    dm.extract_dailymed(raw, out, release_kinds=KINDS)  # part 1: ab1, ab2; part 2: ab3
    _load(db, out, "dailymed_bab__part00001")
    _load(db, out, "dailymed_bab__part00002")
    moved = f"dailymed:{sids[2]}"
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.label_section WHERE article_id = %s", (moved,))
        assert cur.fetchone()[0] == 1
    # ab1 leaves the release (a withdrawal, allowed here): ab3 moves into part 1.
    del members[f"prescription/20240101_{sids[0]}.zip"]
    outer_zip(raw / "dm_spl_release_human_rx_part1.zip", members)
    dm.extract_dailymed(raw, out, force=True, force_reason="test", release_kinds=KINDS)
    _load(db, out, "dailymed_bab__part00001")
    with db.cursor() as cur:
        for table in ("label_section", "label_substance"):
            cur.execute(f"SELECT count(*) FROM episteme.{table} WHERE article_id = %s", (moved,))
            n = cur.fetchone()[0]
            assert n == (1 if table == "label_section" else 3), (table, n)


def test_sidecar_ndc_products_are_9_digit_keys():
    parsed = {"sections": [], "substances": [], "ndcs": ["12345-678", "0093-7214", "x-y"]}
    rows = labels.sidecar_rows("dailymed:a", parsed)
    assert [r["ndc_product"] for r in rows] == ["123450678", "000937214", "x-y"]


def test_migration_0010_label_substance_has_no_resolved_key_and_a_lock_timeout():
    text = (MIGRATIONS / "0010_regulatory_sources.sql").read_text(encoding="utf-8")
    create = text.split("CREATE TABLE IF NOT EXISTS episteme.label_substance", 1)[1]
    create = create.split(");", 1)[0]
    assert "substance_key" not in create
    assert "DROP COLUMN IF EXISTS substance_key" in text
    assert "label_substance (unii)" in text
    begin = text.index("BEGIN;")
    assert text.index("SET LOCAL lock_timeout = '60s';") < text.index("CREATE TABLE", begin)


def test_replace_labels_copies_one_table_at_a_time(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    shard = tmp_path / "staging" / "dailymed" / "dailymed_bab__part00001.parquet"
    shard.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": ["dailymed:a"]}), shard)
    labels.write_sidecar(
        tmp_path,
        "dailymed_bab__part00001",
        [
            {
                "article_id": "dailymed:a",
                "kind": "section",
                "loinc_code": "1",
                "title": "t",
                "start_offset": 0,
                "end_offset": 1,
            },
            {"article_id": "dailymed:a", "kind": "substance", "unii": "U1"},
            {"article_id": "dailymed:a", "kind": "substance", "unii": "U1"},
            {"article_id": "dailymed:a", "kind": "substance", "ndc_product": "123450678"},
        ],
    )
    events: list = []

    class Copy:
        def __init__(self, sql):
            self.sql = sql

        def __enter__(self):
            assert events.count("open") == events.count("close"), "two COPYs at once"
            events.append("open")
            return self

        def __exit__(self, *exc):
            events.append("close")
            return False

        def write_row(self, row):
            events.append((self.sql.split()[1], tuple(row)))

    class Cur(_Cursor):
        def copy(self, sql):
            return Copy(sql)

    class Conn(_Conn):
        def cursor(self):
            return Cur(self.log)

    out = labels.replace_labels(Conn(), shard)
    assert out == {"label_sections": 1, "label_substances": 2}
    rows = [e for e in events if isinstance(e, tuple)]
    assert rows == [
        ("episteme.label_section", ("dailymed:a", "1", "t", 0, 1)),
        ("episteme.label_substance", ("dailymed:a", "U1", None)),
        ("episteme.label_substance", ("dailymed:a", None, "123450678")),
    ]
    assert [e for e in events if isinstance(e, str)] == ["open", "close", "open", "close"]
