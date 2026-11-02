"""FAERS generation loader.

The unmarked tests are pure. The pg-marked tests run against ``episteme_test`` only;
the schema is built once per module and each test clears the data. ``post_load`` stays
``None`` here: the tests observe the steps by wrapping the module's internal functions.
"""

import json
import os
import re
import zipfile
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.data.regulatory.faers import load as fl
from episteme.data.regulatory.normalize import MAX_INDEX_TEXT_BYTES

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/episteme/data/db/migrations"
FP_A, FP_B = "a" * 64, "b" * 64
GEN_A, GEN_B = "g" + "a" * 10, "g" + "b" * 10


def _report(
    report_id,
    *,
    version=None,
    received="20200101",
    receipt="20200102",
    drugs=("DRUG A",),
    terms=("TERM A",),
):
    report = {
        "safetyreportid": report_id,
        "serious": "1",
        "patient": {
            "drug": [{"medicinalproduct": d, "drugcharacterization": "1"} for d in drugs],
            "reaction": [{"reactionmeddrapt": t} for t in terms],
        },
    }
    if received is not None:
        report["receivedate"] = received
    if receipt is not None:
        report["receiptdate"] = receipt
    if version is not None:
        report["safetyreportversion"] = str(version)
    return report


def _zip(raw: Path, quarter: str, reports: list[dict], n: int = 1, of: int = 1) -> Path:
    """A zip named as openFDA names it; by default the whole of its quarter."""
    path = raw / "drug" / "event" / quarter / f"drug-event-{n:04d}-of-{of:04d}.json.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(path.name.removesuffix(".zip"), json.dumps({"meta": {}, "results": reports}))
    return path


# ---------------------------------------------------------------------------
# pure: the decision
# ---------------------------------------------------------------------------
def test_first_run_starts_a_fresh_generation():
    d = fl.decide_generation(FP_A, live=None, unfinished={}, force=False)
    assert (d.action, d.generation, d.drop) == ("fresh", None, [])


def test_matching_unfinished_generation_is_resumed():
    d = fl.decide_generation(FP_A, live=None, unfinished={GEN_A: FP_A}, force=False)
    assert (d.action, d.generation, d.drop) == ("resume", GEN_A, [])


def test_changed_inputs_drop_the_unfinished_generation():
    d = fl.decide_generation(FP_B, live=None, unfinished={GEN_A: FP_A}, force=False)
    assert (d.action, d.generation, d.drop) == ("fresh", None, [GEN_A])


def test_inputs_equal_to_the_live_generation_are_current():
    d = fl.decide_generation(FP_A, live=FP_A, unfinished={GEN_B: FP_B}, force=False)
    assert (d.action, d.generation, d.drop) == ("current", None, [GEN_B])


def test_force_starts_fresh_even_when_it_could_resume_or_is_current():
    d = fl.decide_generation(FP_A, live=FP_A, unfinished={GEN_A: FP_A}, force=True)
    assert (d.action, d.generation, d.drop) == ("fresh", None, [GEN_A])


def test_unfinished_generation_with_no_fingerprint_is_dropped():
    d = fl.decide_generation(FP_A, live=None, unfinished={GEN_A: None}, force=False)
    assert (d.action, d.generation, d.drop) == ("fresh", None, [GEN_A])


# ---------------------------------------------------------------------------
# pure: names, fingerprint, batches, discovery
# ---------------------------------------------------------------------------
def test_generation_id_matches_the_catalog_pattern():
    gen = fl.new_generation_id()
    assert re.fullmatch(fl.GENERATION_RE, gen)
    assert gen != fl.new_generation_id()


def test_every_generated_name_fits_a_postgres_identifier():
    gen = fl.new_generation_id()
    for parent, specs in fl.INDEXES.items():
        leaf = fl.leaf_table(parent, gen, "_default")
        for suffix, _ in specs:
            assert len(f"{leaf}_{suffix}".encode()) <= 63
        assert len(fl.old_leaf_table(parent, gen, "_default").encode()) <= 63
    assert len(fl.state_table(gen)) <= 63


def test_six_spec_indexes_and_the_batch_norm_index_are_listed():
    specs = {parent: [s for _, s in v] for parent, v in fl.INDEXES.items()}
    assert specs["faers_report"] == ["(safetyreportid)"]
    assert set(specs["faers_drug"]) == {
        "(drug_entry_id)",
        "(safetyreportid)",
        "(drug_entry_id) WHERE batch_number IS NOT NULL",
        "(batch_norm) WHERE batch_norm IS NOT NULL",
    }
    assert set(specs["faers_reaction"]) == {"(term_id)", "(safetyreportid)"}


@pytest.mark.parametrize(
    ("bound", "values"),
    [
        ("FOR VALUES FROM (2004) TO (2005)", (2004, 2005)),
        ("DEFAULT", None),
    ],
)
def test_partition_bound_parses(bound, values):
    assert fl.parse_bound(bound) == values


def test_unexpected_partition_bound_is_refused():
    with pytest.raises(ValueError, match="partition bound"):
        fl.parse_bound("FOR VALUES IN (1)")


def test_fingerprint_follows_key_size_and_mtime(tmp_path):
    raw = tmp_path / "raw"
    a = _zip(raw, "2020q1", [_report("1")])
    first = fl.fingerprint([(a, "k")])
    assert first == fl.fingerprint([(a, "k")])
    assert first != fl.fingerprint([(a, "other")])
    os.utime(a, ns=(1, 1))
    assert first != fl.fingerprint([(a, "k")])


def test_batch_is_full_at_the_byte_cap():
    batch = fl.Batch(cap_bytes=200)
    assert not batch.full
    batch.add({"safetyreportid": "x" * 10}, [], [])
    assert not batch.full
    batch.add({"safetyreportid": "x"}, [{"indication": "y" * 60}], [])
    assert batch.full
    assert (len(batch.reports), len(batch.drugs), len(batch.reactions)) == (2, 1, 0)
    batch.clear()
    assert not batch.full and batch.reports == [] and batch.drugs == []


def test_faers_zips_reads_drug_event_only_in_path_order(tmp_path):
    raw = tmp_path / "raw"
    b = _zip(raw, "2020q2", [])
    a = _zip(raw, "2020q1", [])
    other = raw / "drug" / "label" / "x.zip"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    assert fl.faers_zips(raw) == [a, b]
    assert fl.faers_zips(tmp_path / "nothing") == []


def test_inputs_with_one_input_key_stop_the_load(tmp_path):
    raw = tmp_path / "raw"
    a = _zip(tmp_path / "a", "q", [])  # both outside raw: the key is the base name
    b = _zip(tmp_path / "b", "q", [])
    with pytest.raises(ValueError, match="share an input_key"):
        fl.load_generation(None, [a, b], raw_dir=raw, processed_dir=tmp_path, run_id="r")


def test_marker_keys_differ_for_one_file_name_in_two_quarters(tmp_path):
    raw = tmp_path / "raw"
    a = _zip(raw, "2020q1", [])
    b = _zip(raw, "2020q2", [])
    assert a.name == b.name
    keys = fl.input_keys([a, b], raw)
    assert keys[0] != keys[1]
    assert keys[0] == f"drug__event__2020q1__{a.name}"


def test_no_inputs_is_refused(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        fl.load_generation(None, [], raw_dir=tmp_path, processed_dir=tmp_path, run_id="r")


def test_main_refuses_force_without_a_reason(capsys):
    assert fl.main(["--force"]) == 2
    assert "--reason" in capsys.readouterr().err


def test_main_refuses_allow_partial_without_a_reason(capsys):
    assert fl.main(["--allow-partial"]) == 2
    assert "--reason" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# pure: the input set and the floor
# ---------------------------------------------------------------------------
def _names(*names):
    return [Path("raw/drug/event") / n for n in names]


def test_a_complete_input_set_has_no_problems():
    paths = _names(
        "2020q1/drug-event-0001-of-0002.json.zip",
        "2020q1/drug-event-0002-of-0002.json.zip",
        "2020q2/drug-event-0001-of-0001.json.zip",
    )
    assert fl.check_input_set(paths) == []


def test_a_missing_part_is_a_problem():
    problems = fl.check_input_set(
        _names(
            "2020q1/drug-event-0001-of-0003.json.zip",
            "2020q1/drug-event-0003-of-0003.json.zip",
        )
    )
    assert len(problems) == 1
    assert "2020q1" in problems[0] and "missing" in problems[0] and "2" in problems[0]


def test_two_totals_in_one_quarter_are_a_problem():
    # A stale file of the old issue next to the new issue.
    problems = fl.check_input_set(
        _names(
            "2020q1/drug-event-0001-of-0001.json.zip",
            "2020q1/drug-event-0001-of-0002.json.zip",
            "2020q1/drug-event-0002-of-0002.json.zip",
        )
    )
    assert len(problems) == 1
    assert "2020q1" in problems[0] and "several totals" in problems[0]


def test_a_part_above_its_total_and_an_unknown_name_are_problems():
    problems = fl.check_input_set(
        _names(
            "2020q1/drug-event-0002-of-0001.json.zip",
            "2020q2/notes.zip",
        )
    )
    assert len(problems) == 2
    assert any("2020q1" in p for p in problems) and any("notes.zip" in p for p in problems)


def test_a_partial_input_set_is_refused_before_any_database_work(tmp_path):
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_report("1")], 1, 2)
    with pytest.raises(fl.InputSetError, match="--allow-partial"):
        fl.load_generation(None, [z], raw_dir=raw, processed_dir=tmp_path, run_id="r")


@pytest.mark.parametrize(
    ("reports", "live", "share", "below"),
    [
        (89, 100, 0.9, True),
        (90, 100, 0.9, False),
        (1, 0, 0.9, False),  # no live generation, no floor
        (100, 100, 1.0, False),
    ],
)
def test_floor(reports, live, share, below):
    assert fl.below_floor(reports, live, share) is below


# ---------------------------------------------------------------------------
# pure: the format version
# ---------------------------------------------------------------------------
def test_a_changed_format_version_changes_the_fingerprint(tmp_path, monkeypatch):
    z = _zip(tmp_path / "raw", "2020q1", [_report("1")])
    first = fl.fingerprint([(z, "k")])
    monkeypatch.setattr(fl, "FORMAT_VERSION", fl.FORMAT_VERSION + 1)
    second = fl.fingerprint([(z, "k")])
    assert first != second
    # The unfinished generation is dropped, and the live one is not current.
    d = fl.decide_generation(second, live=first, unfinished={GEN_A: first}, force=False)
    assert (d.action, d.generation, d.drop) == ("fresh", None, [GEN_A])


# ---------------------------------------------------------------------------
# pure: the swap's errors, and the failed drop
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        if not isinstance(query, str):  # the composed LOCK TABLE
            raise psycopg.errors.DeadlockDetected("deadlock")


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def rollback(self):
        pass

    def commit(self):
        pass


def test_a_deadlock_in_the_swap_is_a_swap_error(monkeypatch):
    monkeypatch.setattr(fl, "_check_access", lambda *a: None)
    with pytest.raises(fl.GenerationSwapError, match="old generation still serves"):
        fl._swap(_FakeConn(), GEN_A, FP_A, {"reports": 1}, {"fingerprint": None, "reports": 0}, "r")


def test_a_failed_old_generation_drop_prints_the_retry_action(monkeypatch, capsys):
    monkeypatch.setattr(fl, "_drop_tables", lambda conn, names: False)
    assert fl._drop_old_generation(None, GEN_A) is False
    err = capsys.readouterr().err
    assert "WARNING" in err and "run the load again" in err and GEN_A in err


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


def _generation_relations(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'episteme' AND c.relkind IN ('r', 'p') AND c.relname ~ %s "
            "ORDER BY 1",
            (r"^faers_(gen_g|(report|drug|reaction)_(old_)?g[0-9a-f]{10})",),
        )
        return [r[0] for r in cur.fetchall()]


@pytest.fixture
def db(_schema, pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "audit_root"))
    from episteme.config import get_settings

    get_settings.cache_clear()
    with pg_conn.cursor() as cur:
        # Parents first: a swap renames them, so drop by the live catalog.
        for name in _generation_relations(pg_conn):
            cur.execute(f"DROP TABLE IF EXISTS episteme.{name} CASCADE")
        cur.execute(
            "TRUNCATE episteme.faers_report, episteme.faers_drug, episteme.faers_reaction, "
            "episteme.faers_drug_entry, episteme.faers_term, episteme.faers_entry_substance"
        )
        cur.execute("COMMENT ON TABLE episteme.faers_report IS NULL")
    pg_conn.commit()
    yield pg_conn
    pg_conn.rollback()
    get_settings.cache_clear()


@pytest.fixture
def reader():
    conn = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=True)
    yield conn
    conn.close()


def _run(conn, tmp_path, zips, **kw):
    return fl.load_generation(
        conn,
        zips,
        raw_dir=tmp_path / "raw",
        processed_dir=tmp_path / "proc",
        run_id="t",
        **kw,
    )


def _all(conn, query, params=None):
    """All rows of a read; ends the read transaction, so that no lock stays behind."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.rollback()
    return rows


def _ids(conn):
    return [r[0] for r in _all(conn, "SELECT safetyreportid FROM episteme.faers_report ORDER BY 1")]


def _unfinished(conn):
    out = fl.unfinished_generations(conn)
    conn.rollback()
    return out


@pytest.mark.pg
def test_generation_swap_is_atomic(db, reader, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("old")])])
    seen = {}
    real_swap = fl._swap

    def watched(conn, generation, *a, **k):
        # The new generation is complete; a reader on another connection still has the old.
        new_table = fl.generation_table("faers_report", generation)
        seen["before"] = _ids(reader)
        seen["built"] = _all(reader, f"SELECT safetyreportid FROM episteme.{new_table}")
        out = real_swap(conn, generation, *a, **k)
        seen["after"] = _ids(reader)
        return out

    monkeypatch.setattr(fl, "_swap", watched)
    _run(db, tmp_path, [_zip(raw, "2020q2", [_report("new")])])
    assert seen["before"] == ["old"]
    assert seen["built"] == [("new",)]
    assert seen["after"] == ["new"]


@pytest.mark.pg
def test_a_failed_zip_leaves_the_old_generation_serving(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("old")])])
    bad = raw / "drug" / "event" / "2020q2" / "drug-event-0001-of-0001.json.zip"
    bad.parent.mkdir(parents=True)
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("x.json", '{"results": [{"safetyreportid": "cut"')
    with pytest.raises(Exception):  # noqa: B017 - the JSON library's own error type
        _run(db, tmp_path, [bad])
    db.rollback()
    assert _ids(db) == ["old"]


@pytest.mark.pg
def test_a_reader_lock_fails_the_swap_and_a_rerun_goes_straight_to_it(
    db, reader, tmp_path, monkeypatch
):
    monkeypatch.setenv("EPISTEME_FAERS_LOCK_TIMEOUT_S", "1")
    from episteme.config import get_settings

    get_settings.cache_clear()
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("old")])])
    z = _zip(raw, "2020q2", [_report("new")])

    reader.autocommit = False
    with reader.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.faers_report")  # holds ACCESS SHARE
    with pytest.raises(fl.GenerationSwapError, match="EPISTEME_FAERS_LOCK_TIMEOUT_S"):
        _run(db, tmp_path, [z])
    reader.rollback()
    reader.autocommit = True
    db.rollback()
    assert _ids(reader) == ["old"]
    assert len(_unfinished(db)) == 1

    def must_not_read(_path):
        raise AssertionError("a finished zip was read again")

    monkeypatch.setattr(fl, "iter_reports", must_not_read)
    monkeypatch.setattr(fl, "_build_indexes", lambda *a, **k: pytest.fail("indexes built twice"))
    out = _run(db, tmp_path, [z])
    assert out["status"] == "swapped" and out["zips_skipped"] == 1
    assert _ids(db) == ["new"]


def _access(conn):
    return _all(
        conn,
        "SELECT relname, relowner::regrole::text, relacl::text[] FROM pg_class "
        "WHERE oid IN ('episteme.faers_report'::regclass, 'episteme.faers_drug'::regclass, "
        "'episteme.faers_reaction'::regclass) ORDER BY 1",
    )


@pytest.mark.pg
def test_a_new_parent_with_other_privileges_is_refused_before_the_copy(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    with db.cursor() as cur:
        cur.execute("GRANT SELECT ON episteme.faers_report TO PUBLIC")
    db.commit()
    monkeypatch.setattr(fl, "iter_reports", lambda _p: pytest.fail("the COPY started"))
    try:
        with pytest.raises(fl.GenerationError, match="owner or"):
            _run(db, tmp_path, [_zip(raw, "2020q1", [_report("1")])])
    finally:
        db.rollback()
        with db.cursor() as cur:
            cur.execute("REVOKE SELECT ON episteme.faers_report FROM PUBLIC")
        db.commit()


@pytest.mark.pg
def test_parents_carry_the_plain_names_and_prune_after_a_swap(db, tmp_path):
    raw = tmp_path / "raw"
    access_before = _access(db)
    report = _report("r24", received="20240301", receipt="20240302", drugs=("X",))
    out = _run(db, tmp_path, [_zip(raw, "2020q1", [report])])
    assert out["old_generation_dropped"] is True
    assert _generation_relations(db) == []
    assert access_before == _access(db)

    # A second generation: the plain names again, and only the new token on the indexes.
    second = _run(db, tmp_path, [_zip(raw, "2020q2", [report])])
    assert second["generation"] != out["generation"]
    assert second["old_generation_dropped"] is True
    assert _generation_relations(db) == []
    assert access_before == _access(db)
    index_names = {
        r[0]
        for r in _all(
            db,
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename = 'faers_drug_y2024'",
        )
    }
    assert index_names == {
        f"faers_drug_{second['generation']}_y2024_{suffix}"
        for suffix, _ in fl.INDEXES["faers_drug"]
    }
    assert _all(
        db,
        "SELECT count(*) FROM pg_indexes WHERE schemaname = 'episteme' AND indexname ~ %s",
        (out["generation"],),
    ) == [(0,)]
    db.rollback()
    leaf_names = _all(
        db,
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = 'episteme.faers_drug'::regclass",
    )
    assert sorted(r[0] for r in leaf_names) == sorted(
        [f"faers_drug_y{y}" for y in range(2004, 2031)] + ["faers_drug_default"]
    )
    plan = "\n".join(
        r[0]
        for r in _all(db, "EXPLAIN SELECT * FROM episteme.faers_drug WHERE receive_year >= 2024")
    )
    for year in (2024, 2030):
        assert f"faers_drug_y{year}" in plan
    for year in (2004, 2023):
        assert f"faers_drug_y{year}" not in plan
    assert _all(db, "SELECT tableoid::regclass::text FROM episteme.faers_drug")[0][0].endswith(
        "faers_drug_y2024"
    )


@pytest.mark.pg
def test_duplicate_id_keeps_highest_version_then_latest_receipt(db, tmp_path):
    raw = tmp_path / "raw"
    zips = [
        _zip(raw, "2020q1", [_report("v", version=3, drugs=("V3",))], 1),
        _zip(raw, "2020q2", [_report("v", version=2, receipt="20201231", drugs=("V2",))], 1),
        _zip(raw, "2020q3", [_report("r", version=1, receipt="20200101", drugs=("OLD R",))], 1),
        _zip(raw, "2020q4", [_report("r", version=1, receipt="20200301", drugs=("NEW R",))], 1),
        # A missing version ranks below any version; a missing receipt below any receipt.
        _zip(raw, "2021q1", [_report("n", drugs=("NO VERSION",))], 1),
        _zip(raw, "2021q2", [_report("n", version=1, receipt=None, drugs=("VERSION 1",))], 1),
        _zip(raw, "2021q3", [_report("f", version=1, drugs=("EARLY FILE",))], 1),
        _zip(raw, "2021q4", [_report("f", version=1, drugs=("LATE FILE",))], 1),
    ]
    out = _run(db, tmp_path, zips)
    assert out["duplicates_removed"] == 4
    assert out["reports"] == 4
    rows = dict(
        _all(
            db,
            "SELECT d.safetyreportid, e.product_name FROM episteme.faers_drug d "
            "JOIN episteme.faers_drug_entry e USING (drug_entry_id)",
        )
    )
    assert rows == {"v": "V3", "r": "NEW R", "n": "VERSION 1", "f": "LATE FILE"}
    # The losers' child rows are gone too.
    assert _all(db, "SELECT count(*) FROM episteme.faers_reaction") == [(4,)]


@pytest.mark.pg
def test_duplicate_id_across_two_year_partitions_keeps_one_copy(db, tmp_path):
    raw = tmp_path / "raw"
    zips = [
        _zip(raw, "2020q1", [_report("x", version=1, received="20150101", drugs=("A",))]),
        _zip(raw, "2020q2", [_report("x", version=2, received="20200101", drugs=("B",))]),
        _zip(raw, "2020q3", [_report("x", version=3, received=None, receipt=None, drugs=("C",))]),
    ]
    out = _run(db, tmp_path, zips)
    assert out["duplicates_cross_year"] == 2
    assert _all(db, "SELECT version, receive_year FROM episteme.faers_report") == [(3, None)]
    assert _all(db, "SELECT count(*) FROM episteme.faers_drug") == [(1,)]


@pytest.mark.pg
def test_id_repeated_in_one_zip_keeps_the_first_and_is_counted(db, tmp_path):
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_report("x", version=1, drugs=("A",)), _report("x", version=2)])
    out = _run(db, tmp_path, [z])
    assert out["duplicates_in_zip"] == 1 and out["duplicates_removed"] == 0
    assert _all(db, "SELECT version FROM episteme.faers_report") == [(1,)]
    assert _all(db, "SELECT count(*) FROM episteme.faers_drug") == [(1,)]


@pytest.mark.pg
def test_report_absent_from_new_issue_disappears(db, tmp_path):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("a"), _report("b")])])
    assert _ids(db) == ["a", "b"]
    # A real withdrawal this large is below the floor, so it takes the override.
    _run(db, tmp_path, [_zip(raw, "2020q2", [_report("a")])], override_reason="withdrawn")
    assert _ids(db) == ["a"]
    assert _all(db, "SELECT DISTINCT safetyreportid FROM episteme.faers_drug") == [("a",)]


@pytest.mark.pg
def test_resume_skips_completed_zips(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    one = _zip(raw, "2020q1", [_report("1")])
    two = _zip(raw, "2020q2", [_report("2")])
    real = fl.iter_reports

    def fail_on_two(path):
        if path == two:
            raise RuntimeError("stop here")
        return real(path)

    monkeypatch.setattr(fl, "iter_reports", fail_on_two)
    with pytest.raises(RuntimeError, match="stop here"):
        _run(db, tmp_path, [one, two])
    db.rollback()
    assert _ids(db) == []
    assert len(_unfinished(db)) == 1
    key_one = fl.input_keys([one], raw)[0]
    marker = tmp_path / "proc/_ops/openfda_faers/load_success" / f"{key_one}.ok"
    assert marker.is_file()

    read = []

    def spy(path):
        read.append(path)
        return real(path)

    monkeypatch.setattr(fl, "iter_reports", spy)
    out = _run(db, tmp_path, [one, two])
    assert read == [two]
    assert out["zips_skipped"] == 1 and out["zips_loaded"] == 1
    assert out["reports_read"] == 2  # the counts of the earlier run are kept
    assert _ids(db) == ["1", "2"]


@pytest.mark.pg
def test_changed_inputs_start_fresh_and_drop_the_unfinished_generation(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    one = _zip(raw, "2020q1", [_report("1")])
    two = _zip(raw, "2020q2", [_report("2")])
    real = fl.iter_reports

    def fail_on_two(path):
        if path == two:
            raise RuntimeError("stop here")
        return real(path)

    monkeypatch.setattr(fl, "iter_reports", fail_on_two)
    with pytest.raises(RuntimeError):
        _run(db, tmp_path, [one, two])
    db.rollback()
    first = list(_unfinished(db))
    monkeypatch.setattr(fl, "iter_reports", real)

    _zip(raw, "2020q2", [_report("2"), _report("3")])  # a new issue of the file
    out = _run(db, tmp_path, [one, two])
    assert out["generation"] not in first
    assert out["zips_loaded"] == 2 and out["zips_skipped"] == 0
    assert _generation_relations(db) == []
    db.rollback()
    assert _ids(db) == ["1", "2", "3"]


@pytest.mark.pg
def test_commit_without_its_marker_is_not_loaded_twice(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_report("1")])
    real_write = fl.checkpoint_markers.write_marker

    def crash(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(fl.checkpoint_markers, "write_marker", crash)
    with pytest.raises(OSError, match="disk gone"):
        _run(db, tmp_path, [z])
    db.rollback()
    monkeypatch.setattr(fl.checkpoint_markers, "write_marker", real_write)
    out = _run(db, tmp_path, [z])
    assert out["zips_skipped"] == 1 and out["zips_loaded"] == 0
    assert _all(db, "SELECT count(*) FROM episteme.faers_report") == [(1,)]
    marker = tmp_path / "proc/_ops/openfda_faers/load_success" / f"{fl.input_keys([z], raw)[0]}.ok"
    assert marker.is_file()


@pytest.mark.pg
def test_same_inputs_after_a_swap_are_current_and_force_rebuilds(db, tmp_path):
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_report("1")])
    first = _run(db, tmp_path, [z])
    assert first["status"] == "swapped"
    again = _run(db, tmp_path, [z])
    assert again["status"] == "current"
    forced = _run(db, tmp_path, [z], force=True)
    assert forced["status"] == "swapped" and forced["generation"] != first["generation"]
    assert _ids(db) == ["1"]


@pytest.mark.pg
def test_indexes_exist_only_after_the_load_step(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    seen = {}
    real_dedupe = fl._dedupe

    def watched(conn, generation):
        seen["generation"] = generation
        seen["during_load"] = _all(
            conn,
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename ~ %s AND indexname NOT LIKE 'faers_gen%%'",
            (rf"^faers_(report|drug|reaction)_{generation}",),
        )
        return real_dedupe(conn, generation)

    monkeypatch.setattr(fl, "_dedupe", watched)
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("1")])])
    assert seen["during_load"] == []
    # After the swap: per partition, none on the parents.
    names = {
        r[0]
        for r in _all(
            db,
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename = 'faers_drug_y2020'",
        )
    }
    assert names == {
        f"faers_drug_{seen['generation']}_y2020_{suffix}" for suffix, _ in fl.INDEXES["faers_drug"]
    }
    for parent in fl.PARENTS:
        assert _all(
            db,
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'episteme' AND tablename = %s",
            (parent,),
        ) == [(0,)]


@pytest.mark.pg
def test_parent_statistics_are_collected_before_the_swap(db, tmp_path):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("1")])])
    assert (
        _all(
            db,
            "SELECT count(*) FROM pg_stats WHERE schemaname = 'episteme' "
            "AND tablename = 'faers_report' AND inherited",
        )[0][0]
        > 0
    )


@pytest.mark.pg
def test_report_without_a_date_goes_to_the_default_partition_and_is_counted(db, tmp_path):
    raw = tmp_path / "raw"
    z = _zip(
        raw,
        "2020q1",
        [
            _report("dated"),
            _report("none", received=None, receipt=None),
            _report("old", received="19990101"),
        ],
    )
    out = _run(db, tmp_path, [z])
    assert out["no_date"] == 1
    assert out["reports_no_date"] == 1
    assert out["reports_in_default"] == 2  # no date, and a year off the ladder
    rows = _all(
        db,
        "SELECT safetyreportid FROM episteme.faers_report_default ORDER BY 1",
    )
    assert rows == [("none",), ("old",)]
    assert _all(db, "SELECT safetyreportid FROM episteme.faers_drug_default ORDER BY 1") == rows


@pytest.mark.pg
def test_report_without_an_id_is_skipped_and_counted(db, tmp_path):
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_report(""), _report("1")])
    out = _run(db, tmp_path, [z])
    assert out["no_id"] == 1 and out["reports"] == 1


@pytest.mark.pg
def test_dictionaries_only_grow_and_ids_stay_stable(db, tmp_path):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("1", drugs=("A", "B"), terms=("T1",))])])
    entries = dict(_all(db, "SELECT product_name, drug_entry_id FROM episteme.faers_drug_entry"))
    terms = dict(_all(db, "SELECT term, term_id FROM episteme.faers_term"))
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.faers_entry_substance VALUES (%s, 1, 'annotation')",
            (entries["A"],),
        )
    db.commit()
    # The new issue has neither "A" nor "T1".
    _run(db, tmp_path, [_zip(raw, "2020q2", [_report("2", drugs=("B", "C"), terms=("T2",))])])
    after = dict(_all(db, "SELECT product_name, drug_entry_id FROM episteme.faers_drug_entry"))
    assert after["A"] == entries["A"] and after["B"] == entries["B"] and "C" in after
    assert dict(_all(db, "SELECT term, term_id FROM episteme.faers_term"))["T1"] == terms["T1"]
    assert _all(db, "SELECT count(*) FROM episteme.faers_entry_substance") == [(1,)]
    assert _all(
        db,
        "SELECT e.product_name FROM episteme.faers_drug d "
        "JOIN episteme.faers_drug_entry e USING (drug_entry_id) ORDER BY 1",
    ) == [("B",), ("C",)]


@pytest.mark.pg
def test_rows_are_typed_and_joined_through_the_dictionaries(db, tmp_path):
    raw = tmp_path / "raw"
    fixture = json.loads(
        (REPO_ROOT / "tests/data/fixtures/faers/2026q2.json").read_text(encoding="utf-8")
    )
    out = _run(db, tmp_path, [_zip(raw, "2026q2", fixture)])
    assert out["reports"] == 3
    rows = _all(
        db,
        "SELECT d.seq, d.role, e.product_name, e.active_substance, d.batch_number, d.batch_norm, "
        "       d.receive_year, d.source_file "
        "FROM episteme.faers_drug d JOIN episteme.faers_drug_entry e USING (drug_entry_id) "
        "WHERE d.safetyreportid = '26933255' ORDER BY d.seq",
    )
    key = "drug__event__2026q2__drug-event-0001-of-0001.json.zip"
    assert rows[0] == (1, "suspect", "DUPIXENT", "DUPILUMAB", "5F841A", "5F841A", 2026, key)
    assert rows[1][1] == "concomitant"
    terms = _all(
        db,
        "SELECT t.term, r.outcome FROM episteme.faers_reaction r "
        "JOIN episteme.faers_term t USING (term_id) "
        "WHERE r.safetyreportid = '26905497' ORDER BY r.seq",
    )
    assert terms == [("Corneal opacity", "6"), ("Corneal deposits", "3")]


@pytest.mark.pg
def test_a_tiny_byte_cap_gives_the_same_rows(db, tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_LOAD_BATCH_BYTES", "1")
    from episteme.config import get_settings

    get_settings.cache_clear()
    raw = tmp_path / "raw"
    reports = [_report(str(i), drugs=("A", "B"), terms=("T",)) for i in range(5)]
    out = _run(db, tmp_path, [_zip(raw, "2020q1", reports)])
    assert out["reports"] == 5 and out["drug_rows"] == 10 and out["reaction_rows"] == 5
    assert _all(db, "SELECT count(*) FROM episteme.faers_drug_entry") == [(2,)]


@pytest.mark.pg
def test_a_term_too_long_to_index_is_counted_and_kept_without_an_id(db, tmp_path):
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_report("1", terms=("x" * 3000, "short"))])
    out = _run(db, tmp_path, [z])
    assert out["long_terms"] == 1
    assert _all(db, "SELECT seq, term_id IS NULL FROM episteme.faers_reaction ORDER BY 1") == [
        (1, True),
        (2, False),
    ]


@pytest.mark.pg
def test_an_issue_with_no_reports_is_not_swapped_in(db, tmp_path):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("old")])])
    with pytest.raises(fl.GenerationError, match="no reports"):
        _run(db, tmp_path, [_zip(raw, "2020q2", [])])
    db.rollback()
    assert _ids(db) == ["old"]


@pytest.mark.pg
def test_a_loaded_zip_writes_one_audit_row_and_the_swap_another(db, tmp_path):
    raw = tmp_path / "raw"
    out = _run(db, tmp_path, [_zip(raw, "2020q1", [_report("1")])])
    rows = _all(
        db,
        "SELECT event_type, object, rows_affected FROM episteme._audit "
        "WHERE run_id = 't' ORDER BY seq DESC LIMIT 2",
    )
    assert rows[0][0] == "load_replace" and rows[0][2] == 1
    assert out["generation"] in str(rows[0][1])
    old_value, new_value = _all(
        db,
        "SELECT old_value, new_value FROM episteme._audit "
        "WHERE run_id = 't' AND event_type = 'load_replace' ORDER BY seq DESC LIMIT 1",
    )[0]
    assert old_value == {"fingerprint": None, "reports": 0}
    assert new_value["generation"] == out["generation"]
    assert new_value["reports"] == 1
    for key in ("duplicates_removed", "duplicates_cross_year", "duplicates_in_zip", "no_date"):
        assert new_value[key] == 0, key
    assert rows[1][0] == "load_commit" and rows[1][1].startswith("drug__event__2020q1__")


@pytest.mark.pg
def test_the_audit_row_of_a_swap_holds_the_old_generation_and_the_removals(db, tmp_path):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("a")])])
    live_print = _all(db, "SELECT obj_description('episteme.faers_report'::regclass, 'pg_class')")
    zips = [
        _zip(raw, "2020q2", [_report("a", version=1)]),
        _zip(raw, "2020q3", [_report("a", version=2), _report("n", received=None, receipt=None)]),
    ]
    _run(db, tmp_path, zips)
    old_value, new_value = _all(
        db,
        "SELECT old_value, new_value FROM episteme._audit "
        "WHERE run_id = 't' AND event_type = 'load_replace' ORDER BY seq DESC LIMIT 1",
    )[0]
    assert old_value == {"fingerprint": live_print[0][0], "reports": 1}
    assert new_value["duplicates_removed"] == 1 and new_value["no_date"] == 1


@pytest.mark.pg
def test_stored_dates_are_the_first_receipt_and_both_raw_values(db, tmp_path):
    raw = tmp_path / "raw"
    z = _zip(raw, "2004q2", [_report("7547238-2", received="20110614", receipt="20040616")])
    _run(db, tmp_path, [z])
    rows = _all(
        db,
        "SELECT receive_year, receive_date::text, receipt_date::text, source_receive_date::text "
        "FROM episteme.faers_report",
    )
    assert rows == [(2004, "2004-06-16", "2004-06-16", "2011-06-14")]


@pytest.mark.pg
def test_a_new_format_version_rebuilds_a_current_generation(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_report("1")])
    first = _run(db, tmp_path, [z])
    assert _run(db, tmp_path, [z])["status"] == "current"
    monkeypatch.setattr(fl, "FORMAT_VERSION", fl.FORMAT_VERSION + 1)
    again = _run(db, tmp_path, [z])
    assert again["status"] == "swapped" and again["generation"] != first["generation"]


@pytest.mark.pg
def test_a_new_format_version_does_not_resume_an_unfinished_generation(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    one = _zip(raw, "2020q1", [_report("1")])
    two = _zip(raw, "2020q2", [_report("2")])
    real = fl.iter_reports

    def fail_on_two(path):
        if path == two:
            raise RuntimeError("stop here")
        return real(path)

    monkeypatch.setattr(fl, "iter_reports", fail_on_two)
    with pytest.raises(RuntimeError):
        _run(db, tmp_path, [one, two])
    db.rollback()
    unfinished = list(_unfinished(db))
    monkeypatch.setattr(fl, "iter_reports", real)
    monkeypatch.setattr(fl, "FORMAT_VERSION", fl.FORMAT_VERSION + 1)
    out = _run(db, tmp_path, [one, two])
    assert out["generation"] not in unfinished
    assert out["zips_loaded"] == 2 and out["zips_skipped"] == 0
    assert _generation_relations(db) == []


@pytest.mark.pg
def test_the_floor_refuses_a_small_issue_and_the_override_passes_it(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report(str(i)) for i in range(10)])])
    small = _zip(raw, "2020q2", [_report("1")])
    with pytest.raises(fl.GenerationError, match="EPISTEME_FAERS_MIN_LIVE_SHARE"):
        _run(db, tmp_path, [small])
    db.rollback()
    assert len(_ids(db)) == 10
    assert len(_unfinished(db)) == 1

    # The override resumes the built generation, and leaves an audit row with its reason.
    monkeypatch.setattr(fl, "iter_reports", lambda _p: pytest.fail("a finished zip was read"))
    out = _run(db, tmp_path, [small], override_reason="FDA withdrew most reports")
    assert out["status"] == "swapped" and out["overrides"] == ["live floor"]
    assert _ids(db) == ["1"]
    rows = _all(
        db,
        "SELECT object, reason FROM episteme._audit WHERE event_type = 'force_override' "
        "AND run_id = 't' ORDER BY seq DESC LIMIT 1",
    )
    assert rows[0][1] == "FDA withdrew most reports" and "floor" in rows[0][0]


@pytest.mark.pg
def test_the_share_comes_from_the_settings(db, tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_FAERS_MIN_LIVE_SHARE", "0.05")
    from episteme.config import get_settings

    get_settings.cache_clear()
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report(str(i)) for i in range(10)])])
    assert _run(db, tmp_path, [_zip(raw, "2020q2", [_report("1")])])["overrides"] == []


@pytest.mark.pg
def test_the_override_passes_a_partial_input_set_and_is_audited(db, tmp_path):
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_report("1")], 1, 2)
    out = _run(db, tmp_path, [z], override_reason="one file on purpose")
    assert out["status"] == "swapped" and out["overrides"] == ["input set"]
    rows = _all(
        db,
        "SELECT object, reason FROM episteme._audit WHERE event_type = 'force_override' "
        "AND run_id = 't' ORDER BY seq DESC LIMIT 1",
    )
    assert rows[0][1] == "one file on purpose" and "input set" in rows[0][0]


@pytest.mark.pg
def test_a_sample_run_bypasses_both_checks_with_no_override(db, tmp_path):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report(str(i)) for i in range(10)])])
    z = _zip(raw, "2020q2", [_report("1")], 1, 2)
    out = _run(db, tmp_path, [z], sample=True)
    assert out["status"] == "swapped" and out["overrides"] == []
    assert _ids(db) == ["1"]


@pytest.mark.pg
def test_long_batch_and_long_id_stop_nothing_and_are_counted(db, tmp_path):
    raw = tmp_path / "raw"
    long_batch = "A" * (MAX_INDEX_TEXT_BYTES + 1)
    report = _report("1")
    report["patient"]["drug"][0]["drugbatchnumb"] = long_batch
    too_long_id = _report("9" * (MAX_INDEX_TEXT_BYTES + 1))
    out = _run(db, tmp_path, [_zip(raw, "2020q1", [report, too_long_id])])
    assert out["long_batches"] == 1 and out["long_ids"] == 1 and out["reports"] == 1
    assert _ids(db) == ["1"]
    assert _all(db, "SELECT length(batch_number), batch_norm FROM episteme.faers_drug") == [
        (MAX_INDEX_TEXT_BYTES + 1, None)
    ]


@pytest.mark.pg
def test_post_load_runs_after_the_indexes_and_a_failure_keeps_the_old_generation(
    db, reader, tmp_path
):
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("old")])])
    z = _zip(raw, "2020q2", [_report("new")])
    calls = []

    def callback(conn):
        generation = next(iter(fl.unfinished_generations(conn)))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_indexes WHERE schemaname = 'episteme' AND tablename = %s",
                (fl.leaf_table("faers_report", generation, "_y2020"),),
            )
            indexes = cur.fetchone()[0]
            cur.execute(
                psycopg.sql.SQL("SELECT safetyreportid FROM {}").format(
                    psycopg.sql.Identifier(
                        "episteme", fl.generation_table("faers_report", generation)
                    )
                )
            )
            built = cur.fetchall()
        calls.append((indexes, built, _ids(reader)))
        if len(calls) == 1:
            with conn.cursor() as cur:  # work that the failure must undo
                cur.execute("CREATE TABLE episteme.faers_post_load_probe (x int)")
            raise RuntimeError("the callback failed")

    with pytest.raises(RuntimeError, match="the callback failed"):
        _run(db, tmp_path, [z], post_load=callback)
    db.rollback()
    assert calls == [(1, [("new",)], ["old"])]  # after the indexes, before the swap
    assert _ids(reader) == ["old"]
    assert _all(db, "SELECT to_regclass('episteme.faers_post_load_probe')") == [(None,)]
    assert len(_unfinished(db)) == 1

    out = _run(db, tmp_path, [z], post_load=callback)
    assert out["status"] == "swapped" and out["zips_skipped"] == 1
    assert len(calls) == 2
    assert _ids(reader) == ["new"]


@pytest.mark.pg
def test_post_load_is_not_repeated_after_a_failed_swap(db, reader, tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_FAERS_LOCK_TIMEOUT_S", "1")
    from episteme.config import get_settings

    get_settings.cache_clear()
    raw = tmp_path / "raw"
    _run(db, tmp_path, [_zip(raw, "2020q1", [_report("old")])])
    z = _zip(raw, "2020q2", [_report("new")])
    calls = []
    reader.autocommit = False
    with reader.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.faers_report")
    with pytest.raises(fl.GenerationSwapError):
        _run(db, tmp_path, [z], post_load=lambda conn: calls.append(1))
    reader.rollback()
    reader.autocommit = True
    db.rollback()
    _run(db, tmp_path, [z], post_load=lambda conn: calls.append(1))
    assert calls == [1]
    assert _ids(reader) == ["new"]
