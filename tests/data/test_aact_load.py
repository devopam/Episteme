"""AACT (ClinicalTrials.gov) loader: release id, CSV reading, dictionary checks, typed dates,
markers, and the release-schema load with its view switch.

The unmarked tests are pure and build tiny fixture zips in ``tmp_path``. The pg-marked tests
run against ``episteme_test`` only; the schema is built once per module, and each test drops
the ``aact`` schemas and clears the substance tables.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.data.aact import dictionary as dct
from episteme.data.aact import load_aact as la

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/episteme/data/db/migrations"

ZIP_NAME = "20261103_export_ctgov.zip"


def _zip(path: Path, members: dict[str, str]) -> Path:
    """A zip with one member per table; each value is the member's text, as written. The
    bytes and the modification time follow the content, so the same members give the same
    input fingerprint and other members another one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 3, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text.encode("utf-8"))
    digest = hashlib.sha256(repr(sorted(members.items())).encode("utf-8")).digest()
    ns = 1_700_000_000_000_000_000 + int.from_bytes(digest[:6], "big") * 1000
    os.utime(path, ns=(ns, ns))
    return path


def _dictionary(rows: list[tuple[str, str, str]]) -> dict[str, list[dict]]:
    """A dictionary of (table, column, type) rows in the shape of ``read_dictionary``."""
    out: dict[str, list[dict]] = {}
    for table, column, type_ in rows:
        out.setdefault(table, []).append(
            {"Table": table, "Column": column, "Type": type_, "Deprecated": "No"}
        )
    return out


# ---------------------------------------------------------------------------
# pure
# ---------------------------------------------------------------------------
def test_release_id_from_zip_name(tmp_path):
    assert la.release_id_from_zip(Path(ZIP_NAME)) == "20261103"
    assert la.release_id_from_zip(tmp_path / "sub" / "20240229_export_ctgov.zip") == "20240229"
    # The names download_aact.sh writes.
    assert la.release_id_from_zip(Path("flatfiles_2026-11-03.zip")) == "20261103"
    assert la.release_id_from_zip(Path("flatfiles_20261103.zip")) == "20261103"
    for bad in (
        "20261340_export_ctgov.zip",
        "export_ctgov.zip",
        "2026110_x.zip",
        "x20261103.zip",
        "flatfiles_2026-02-30.zip",
        "flatfiles_2026-11.zip",
        "postgres_2026-11-03.zip",
    ):
        with pytest.raises(ValueError):
            la.release_id_from_zip(Path(bad))
    assert la.schema_name("20261103") == "aact_r20261103"


def test_quoted_pipe_and_newline_stay_one_field(tmp_path):
    z = _zip(
        tmp_path / ZIP_NAME,
        {
            "brief_summaries.txt": (
                "id|nct_id|description\n"
                '1|NCT00000001|"a | b\nsecond line, with ""quotes"""\n'
                "2|NCT00000002|plain\n"
            )
        },
    )
    rows = list(la.iter_table(z, "brief_summaries.txt"))
    assert rows == [
        ["id", "nct_id", "description"],
        ["1", "NCT00000001", 'a | b\nsecond line, with "quotes"'],
        ["2", "NCT00000002", "plain"],
    ]


def test_crlf_inside_quotes_is_kept(tmp_path):
    z = _zip(tmp_path / ZIP_NAME, {"t.txt": 'id|v\r\n1|"x\r\ny"\r\n'})
    assert list(la.iter_table(z, "t.txt")) == [["id", "v"], ["1", "x\r\ny"]]


def test_header_drives_columns_and_mismatches_are_reported():
    dictionary = _dictionary(
        [
            ("studies", "nct_id", "text"),
            ("studies", "start_date", "date"),
            ("studies", "old_column", "text"),
        ]
    )
    dictionary["studies"][2]["Deprecated"] = "Yes"
    plan = la.plan_table("studies", ["nct_id", "start_date", "new_column"], dictionary)
    # The file header gives the text columns; the typed copy follows them.
    assert plan.columns == ["nct_id", "start_date", "new_column"]
    assert plan.typed == [(1, "start_date" + la.TYPED_DATE_SUFFIX)]
    assert plan.all_columns == ["nct_id", "start_date", "new_column", "start_date_as_date"]
    assert plan.report == {
        "only_in_file": ["new_column"],
        "only_in_dictionary": ["old_column"],
        "deprecated_in_file": [],
    }
    # A table the dictionary does not know still loads, and is reported.
    other = la.plan_table("extra", ["nct_id", "x"], dictionary)
    assert other.columns == ["nct_id", "x"] and other.typed == []
    assert other.report["only_in_file"] == ["nct_id", "x"]


def test_deprecated_columns_are_loaded_and_reported():
    dictionary = _dictionary([("studies", "nct_id", "text"), ("studies", "old", "text")])
    dictionary["studies"][1]["Deprecated"] = "Yes"
    plan = la.plan_table("studies", ["nct_id", "old"], dictionary)
    assert plan.columns == ["nct_id", "old"]
    assert plan.report["deprecated_in_file"] == ["old"]


def test_only_studies_dates_are_typed():
    dictionary = _dictionary([("outcomes", "nct_id", "text"), ("outcomes", "d", "date")])
    assert la.plan_table("outcomes", ["nct_id", "d"], dictionary).typed == []


def test_bad_header_names_are_refused():
    for header in (["nct_id", "nct_id"], ["nct_id", "Bad Name"], ["nct_id", ""]):
        with pytest.raises(ValueError):
            la.plan_table("studies", header, {})
    with pytest.raises(ValueError):
        la.plan_table("studies", ["nct_id", "start_date_as_date"], {})


def test_bad_date_loads_null_and_is_counted():
    dictionary = _dictionary([("studies", "nct_id", "text"), ("studies", "start_date", "date")])
    plan = la.plan_table("studies", ["nct_id", "start_date", "brief_title"], dictionary)
    shaper = la.RowShaper(plan, "studies.txt")
    rows = [
        ["NCT1", "2020-01-31", "a"],
        ["NCT2", "2020-02-30", "b"],  # not a day
        ["NCT3", "20200101", "c"],  # not the ISO form AACT writes
        ["NCT4", "", ""],
        ["NCT5", "2020-W01-1", "e"],
    ]
    out = [shaper.shape(r, line=i + 2) for i, r in enumerate(rows)]
    from datetime import date

    assert out[0] == ("NCT1", "2020-01-31", "a", date(2020, 1, 31))
    assert out[1] == ("NCT2", "2020-02-30", "b", None)
    assert out[2][3] is None and out[4][3] is None
    assert out[3] == ("NCT4", None, None, None)  # an empty field is NULL
    assert shaper.stats["bad_dates"] == {"start_date": 3}
    assert shaper.stats["empty_dates"] == {"start_date": 1}


def test_wrong_width_row_raises_with_its_line():
    plan = la.plan_table("t", ["a", "b"], {})
    shaper = la.RowShaper(plan, "t.txt")
    with pytest.raises(ValueError, match=r"t\.txt.*line 7"):
        shaper.shape(["1"], line=7)
    with pytest.raises(ValueError):
        shaper.shape(["1", "2", "3"], line=8)


def test_nul_is_stripped_and_counted():
    shaper = la.RowShaper(la.plan_table("t", ["a"], {}), "t.txt")
    assert shaper.shape(["x\x00y"], line=2) == ("xy",)
    assert shaper.stats["nul_fields"] == 1


def test_missing_dictionary_tables_are_reported_and_only_the_known_three_are_allowed():
    dictionary = _dictionary(
        [
            ("studies", "nct_id", "text"),
            ("mesh_terms", "id", "integer"),
            ("mesh_headings", "id", "integer"),
            ("study_searches", "id", "integer"),
        ]
    )
    report = la.compare_tables(["studies", "new_table"], dictionary)
    assert report == {
        "missing_from_zip": ["mesh_headings", "mesh_terms", "study_searches"],
        "not_in_dictionary": ["new_table"],
    }
    # The three tables that AACT leaves out of the zip are allowed; a table of the
    # dictionary that the zip leaves out is not.
    assert la.KNOWN_ABSENT_TABLES == ("mesh_headings", "mesh_terms", "study_searches")
    assert la.unexpected_missing(report["missing_from_zip"]) == []
    assert la.unexpected_missing(["conditions", "mesh_terms", "outcomes"]) == [
        "conditions",
        "outcomes",
    ]


def test_the_packaged_dictionary_minus_the_known_three_is_the_tables_floor():
    """The gate floor of ``tables_with_rows_or_upstream_empty`` is the dictionary tables
    less the three that the zip leaves out: absent tables never enter the load state."""
    from episteme.ops import gate

    floor = gate.load_rules("aact")["load"]["min_counts"]["tables_with_rows_or_upstream_empty"]
    assert floor == len(dct.read_dictionary()) - len(la.KNOWN_ABSENT_TABLES) == 49


def test_marker_names_hold_the_database(tmp_path):
    raw = tmp_path / "raw"
    a = raw / "a" / ZIP_NAME
    b = raw / "b" / ZIP_NAME
    names = {
        la.marker_name("episteme", a, raw, "studies"),
        la.marker_name("episteme_test", a, raw, "studies"),
        la.marker_name("episteme", a, raw, "conditions"),
        la.marker_name("episteme", b, raw, "studies"),
    }
    assert len(names) == 4
    name = la.marker_name("episteme_test", a, raw, "studies")
    assert "episteme_test" in name and "a__" + ZIP_NAME in name and name.endswith("__studies")


def test_two_zips_of_one_release_are_refused(tmp_path):
    raw = tmp_path / "raw"
    with pytest.raises(ValueError):
        la.find_release_zip(raw, [raw / "x" / ZIP_NAME, raw / "y" / "20261103_other.zip"])
    # The owner's download and the scripted one of the same day: one release, two zips.
    hand = _zip(raw / ZIP_NAME, {"studies.txt": "nct_id\n"})
    scripted = _zip(raw / "flatfiles_2026-11-03.zip", {"studies.txt": "nct_id\n"})
    with pytest.raises(ValueError, match=r"release 20261103 has 2 zips.*flatfiles_2026-11-03"):
        la.find_release_zip(raw, la.release_zips(raw))
    assert set(la.release_zips(raw)) == {hand, scripted}
    # Also when the doubled release is not the newest.
    _zip(raw / "flatfiles_2026-12-01.zip", {"studies.txt": "nct_id\n"})
    with pytest.raises(ValueError, match="20261103"):
        la.find_release_zip(raw, la.release_zips(raw))


def test_find_release_zip_takes_the_newest_release(tmp_path):
    raw = tmp_path / "raw"
    old = _zip(raw / "20261001_export_ctgov.zip", {"studies.txt": "nct_id\n"})
    new = _zip(raw / ZIP_NAME, {"studies.txt": "nct_id\n"})
    _zip(raw / "postgres_2026-11-04.zip", {"x.txt": "a\n"})  # the database dump: not a release
    assert la.release_zips(raw) == [old, new]
    assert la.find_release_zip(raw, la.release_zips(raw)) == new
    scripted = _zip(raw / "flatfiles_2026-11-04.zip", {"studies.txt": "nct_id\n"})
    assert la.release_zips(raw) == [old, new, scripted]
    assert la.find_release_zip(raw, la.release_zips(raw)) == scripted
    with pytest.raises(FileNotFoundError):
        la.find_release_zip(raw, [])


def test_index_names_fit_and_differ():
    long_table = "t" * 60
    a = la.index_name(long_table, "nct_id")
    b = la.index_name(long_table, "name")
    assert a != b and len(a.encode()) <= 63 and len(b.encode()) <= 63
    assert la.index_name("studies", "nct_id") == "studies_nct_id_idx"


def test_text_indexes_use_a_prefix_so_no_value_is_too_long():
    assert la.index_plan("interventions", ["id", "nct_id", "name"]) == [
        ("nct_id", None),
        ("name", la.INDEX_PREFIX_CHARS),
    ]
    assert la.index_plan("intervention_substance", ["nct_id", "substance_key"]) == [
        ("nct_id", None),
        ("substance_key", None),
    ]
    assert la.index_plan(
        "intervention_substance", ["nct_id", "substance_key", "moiety_key", "method"]
    ) == [("nct_id", None), ("substance_key", None), ("moiety_key", None)]
    assert la.index_plan("conditions", ["id", "name"]) == []  # no such columns, no index
    # 500 characters of up to 4 bytes stay under the btree key limit (about 2700 bytes).
    assert la.INDEX_PREFIX_CHARS * 4 <= 2700
    text = la.index_statement("aact_r20261103", "interventions", "name", 500).as_string(None)
    assert text == (
        'CREATE INDEX IF NOT EXISTS "interventions_name_idx" ON '
        '"aact_r20261103"."interventions" (left("name", 500))'
    )
    whole = la.index_statement("aact_r20261103", "studies", "nct_id", None).as_string(None)
    assert whole.endswith('"aact_r20261103"."studies" ("nct_id")')


def test_row_size_counts_each_field():
    assert la.row_size(["ab", "", "c"]) == 64 + 3 + 3 * 50


def test_name_map_uses_the_resolve_names_key():
    from episteme.data.regulatory import load_substance as ls

    assert la.resolve_key is ls.resolve_key
    assert ls.resolve_key("  Metformin\x00  HYDROCHLORIDE ") == "metformin hydrochloride"
    assert ls.resolve_key(None) == ""


def test_read_dictionary_packaged_copy():
    d = dct.read_dictionary()
    assert len(d) == 52
    assert sum(len(v) for v in d.values()) == 472
    assert d["studies"][0]["Column"] == "nct_id"
    assert {"mesh_headings", "mesh_terms", "study_searches"} <= set(d)
    assert dct.DICTIONARY_DATE == "2026-11-03"
    dates = dct.date_columns(d, "studies")
    assert "start_date" in dates and "completion_date" in dates and "nct_id" not in dates


def test_read_dictionary_from_a_path(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(
        "﻿Section,Category,Table,Column,Type,Nullable,PK,FK,References,Unique,Deprecated,"
        'Description,Example\nP,C,t,c,date,Yes,No,No,,No,No,"x, y",\n',
        encoding="utf-8",
    )
    assert dct.read_dictionary(p) == {
        "t": [
            {
                "Section": "P",
                "Category": "C",
                "Table": "t",
                "Column": "c",
                "Type": "date",
                "Nullable": "Yes",
                "PK": "No",
                "FK": "No",
                "References": "",
                "Unique": "No",
                "Deprecated": "No",
                "Description": "x, y",
                "Example": "",
            }
        ]
    }


def test_orphan_share_setting(monkeypatch):
    from episteme import config as cfg

    monkeypatch.delenv("EPISTEME_AACT_MAX_ORPHAN_SHARE", raising=False)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().aact_max_orphan_share == 0.05
    monkeypatch.setenv("EPISTEME_AACT_MAX_ORPHAN_SHARE", "0.2")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().aact_max_orphan_share == 0.2
    cfg.get_settings.cache_clear()


def test_orphans_over_limit():
    assert la.orphan_breaches({"a": (100, 5), "b": (100, 6), "c": (0, 0)}, 0.05) == {"b": 0.06}


def test_gate_knows_the_aact_load():
    from episteme.ops import gate

    module, wrapper = gate.stage_module("aact", "load")
    assert module == "episteme.data.aact.load_aact"
    assert (gate._SCRIPTS_DATA / wrapper).is_file()


# ---------------------------------------------------------------------------
# pg (run by the controller, not in the cloud sandbox)
# ---------------------------------------------------------------------------
STUDIES = (
    "nct_id|start_date|completion_date|brief_title\n"
    "NCT00000001|2020-01-01|2021-06-30|Metformin in type 2 diabetes\n"
    "NCT00000002|2019-05-01||Aspirin trial\n"
    "NCT00000003|not a date|2022-01-01|A third trial\n"
)
INTERVENTIONS = (
    "id|nct_id|intervention_type|name|description\n"
    '10|NCT00000001|Drug|Metformin Hydrochloride|"500 mg | twice daily\nwith food"\n'
    "11|NCT00000002|Drug|Aspirin|\n"
    "12|NCT00000003|Drug|Unknownium|\n"
)
OTHER_NAMES = "id|nct_id|intervention_id|name\n100|NCT00000002|11|acetylsalicylic acid\n"
CONDITIONS = (
    "id|nct_id|name|downcase_name\n1|NCT00000001|Diabetes|diabetes\n2|NCT00000002|Pain|pain\n"
)
SUMMARIES = 'id|nct_id|description\n1|NCT00000001|"one | two\nthree"\n2|NCT00000002|plain\n'


def _members(**over: str) -> dict[str, str]:
    base = {
        "studies.txt": STUDIES,
        "interventions.txt": INTERVENTIONS,
        "intervention_other_names.txt": OTHER_NAMES,
        "conditions.txt": CONDITIONS,
        "brief_summaries.txt": SUMMARIES,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


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
            _drop_aact(cur)
            cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
            cur.execute((REPO_ROOT / "src/episteme/data/db/extensions.sql").read_text("utf-8"))
            cur.execute((REPO_ROOT / "src/episteme/data/db/schema.sql").read_text("utf-8"))
            for name in ("0002_container_and_book_parts.sql", "0010_regulatory_sources.sql"):
                cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def _drop_aact(cur) -> None:
    cur.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname = 'aact' OR nspname LIKE 'aact\\_r%'"
    )
    for (name,) in cur.fetchall():
        cur.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(name)))


# The tables of the fixture zips. The pg tests load against the packaged dictionary cut to
# these and the three tables AACT leaves out, so a fixture zip is a whole release;
# test_a_dictionary_table_missing_from_the_zip_fails uses the whole dictionary.
FIXTURE_TABLES = (
    "studies",
    "interventions",
    "intervention_other_names",
    "conditions",
    "brief_summaries",
)
_FULL_DICTIONARY = dct.read_dictionary


def _fixture_dictionary() -> dict[str, list[dict]]:
    full = _FULL_DICTIONARY()
    return {t: full[t] for t in (*FIXTURE_TABLES, *la.KNOWN_ABSENT_TABLES)}


@pytest.fixture
def db(_schema, pg_conn, clean_substance, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "proc"))
    monkeypatch.setattr(dct, "read_dictionary", _fixture_dictionary)
    from episteme.config import get_settings

    get_settings.cache_clear()
    with pg_conn.cursor() as cur:
        _drop_aact(cur)
        cur.execute("TRUNCATE episteme.substance_xref, episteme.substance, episteme.reg_release")
        # Metformin hydrochloride (salt) under metformin; aspirin with a synonym.
        cur.execute(
            "INSERT INTO episteme.substance (substance_key, unii, preferred_name) VALUES "
            "(1, 'U1', 'metformin'), (2, 'U2', 'metformin hydrochloride'), (3, 'U3', 'aspirin')"
        )
        cur.execute(
            "UPDATE episteme.substance SET moiety_key = CASE substance_key WHEN 2 THEN 1 "
            "ELSE substance_key END"
        )
        cur.execute(
            "INSERT INTO episteme.substance_xref "
            "(substance_key, id_type, id_value, method, confidence, source) VALUES "
            "(1, 'name', 'metformin', 'normalised_name', 1.0, 'fda_substance'), "
            "(2, 'name', 'metformin hydrochloride', 'normalised_name', 1.0, 'fda_substance'), "
            "(3, 'name', 'aspirin', 'normalised_name', 1.0, 'fda_substance'), "
            "(3, 'name', 'acetylsalicylic acid', 'normalised_name', 1.0, 'fda_substance')"
        )
    pg_conn.commit()
    yield pg_conn
    pg_conn.rollback()
    get_settings.cache_clear()


def _all(conn, query, params=None):
    """All rows of a read; ends the read transaction, so that no lock stays behind."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _load(conn, tmp_path, members=None, name=ZIP_NAME, **kw):
    raw = tmp_path / "raw"
    z = _zip(raw / name, members or _members())
    return la.load_release(conn, z, raw_dir=raw, run_id="t", **kw)


class _RecCursor:
    """Records each statement; answers the few reads that ``_resolve_interventions`` makes."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._last = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.log.append(self._last)

    def fetchall(self):
        return [("aspirin",)]

    def fetchone(self):
        return (7, 3) if "FILTER" in self._last else (3,)

    def copy(self, query):
        self.log.append("COPY")
        return _RecCopy()


class _RecCopy:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        pass


class _RecConn:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def cursor(self):
        return _RecCursor(self.log)


def test_coverage_count_runs_after_the_index_and_analyze(monkeypatch):
    """A new table has no statistics: the index and ANALYZE come before the coverage SELECT
    (without them the count ran as a nested loop for hours)."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    log: list[str] = []
    monkeypatch.setattr(
        la,
        "_columns",
        lambda conn, schema: {
            "interventions": ["id", "nct_id", "name"],
        },
    )
    monkeypatch.setattr(la, "resolve_names", lambda conn, names, source: {"matched": 1})
    monkeypatch.setattr(la.audit_trail, "deferred_mirror", lambda conn: nullcontext())
    monkeypatch.setattr(la.audit_trail, "record", lambda *a, **k: None)
    monkeypatch.setattr(la.audit_trail, "commit", lambda conn: None)
    monkeypatch.setattr(la.audit_trail, "rollback", lambda conn: None)
    ctx = SimpleNamespace(schema="aact_20261103", release_id="20261103", run_id="r")
    out = la._resolve_interventions(_RecConn(log), ctx)

    def first(needle: str) -> int:
        return next(i for i, q in enumerate(log) if needle in q)

    insert, index, analyze, coverage = (
        first("INSERT INTO"),
        first("CREATE INDEX"),
        first('ANALYZE "aact_20261103"'),
        first("FILTER"),
    )
    assert insert < index < analyze < coverage
    assert "(nct_id, intervention_id)" in log[index]
    assert 'ON "aact_20261103"."intervention_substance"' in log[index]
    assert log[analyze].strip().endswith('"aact_20261103"."intervention_substance"')
    assert out["interventions"] == 7 and out["interventions_matched"] == 3


@pytest.mark.pg
def test_release_loads_and_views_switch(db, tmp_path):
    out = _load(db, tmp_path)
    assert out["status"] == "loaded", out
    assert out["release_id"] == "20261103"
    assert out["tables"]["studies"]["rows"] == 3
    assert out["dictionary_date"] == dct.DICTIONARY_DATE
    assert "mesh_terms" in out["dictionary"]["missing_from_zip"]
    assert _all(db, "SELECT count(*) FROM aact.studies") == [(3,)]
    # The quoted pipe and line break stay in one field.
    assert _all(db, "SELECT description FROM aact.brief_summaries WHERE id = '1'") == [
        ("one | two\nthree",)
    ]
    # Typed dates next to the text; a bad one is NULL and counted.
    rows = _all(
        db,
        "SELECT nct_id, start_date, start_date_as_date FROM aact.studies ORDER BY nct_id",
    )
    assert str(rows[0][2]) == "2020-01-01" and rows[2][1] == "not a date" and rows[2][2] is None
    assert out["tables"]["studies"]["stats"]["bad_dates"] == {"start_date": 1}
    assert _all(
        db, "SELECT release_id, status, is_latest FROM episteme.reg_release WHERE dataset='aact'"
    ) == [("20261103", "loaded", True)]
    from episteme.data.regulatory.load_reg import latest_release

    assert latest_release(db, "aact") == "20261103"
    db.rollback()
    # Indexes per release schema.
    idx = {
        r[0]
        for r in _all(
            db, "SELECT indexname FROM pg_indexes WHERE schemaname = %s", ("aact_r20261103",)
        )
    }
    assert {"studies_nct_id_idx", "interventions_name_idx", "conditions_downcase_name_idx"} <= idx
    defs = dict(
        _all(
            db,
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s",
            ("aact_r20261103",),
        )
    )
    # pg_get_indexdef writes the function as "left"(name, 500).
    assert "left" in defs["interventions_name_idx"]
    assert "(name, 500)" in defs["interventions_name_idx"]
    assert "left(" not in defs["studies_nct_id_idx"]
    # The next release switches the views; the old release schema stays.
    out2 = _load(
        db,
        tmp_path,
        _members(
            **{
                "studies.txt": STUDIES.replace(
                    "NCT00000003|not a date|2022-01-01|A third trial\n", ""
                )
            },
            **{"interventions.txt": INTERVENTIONS.replace("12|NCT00000003|Drug|Unknownium|\n", "")},
        ),
        name="20261201_export_ctgov.zip",
    )
    assert out2["status"] == "loaded", out2
    assert _all(db, "SELECT count(*) FROM aact.studies") == [(2,)]
    assert _all(db, "SELECT to_regnamespace('aact_r20261103') IS NOT NULL") == [(True,)]
    assert latest_release(db, "aact") == "20261201"
    db.rollback()


@pytest.mark.pg
def test_repeat_release_skips_or_refuses(db, tmp_path):
    assert _load(db, tmp_path)["status"] == "loaded"
    again = _load(db, tmp_path)
    assert again["status"] == "skipped", again
    # Same release id, other content: refused without --force.
    changed = _members(**{"conditions.txt": CONDITIONS + "3|NCT00000003|Gout|gout\n"})
    refused = _load(db, tmp_path, changed)
    assert refused["status"] == "refused" and "--force" in refused["reason"]
    assert _all(db, "SELECT count(*) FROM aact.conditions") == [(2,)]
    with pytest.raises(ValueError):
        _load(db, tmp_path, changed, force=True)  # --force needs a reason
    forced = _load(db, tmp_path, changed, force=True, reason="corrected snapshot")
    assert forced["status"] == "loaded", forced
    prev = forced["previous_schema"]
    assert prev.startswith("aact_r20261103_prev_")
    assert _all(db, "SELECT count(*) FROM aact.conditions") == [(3,)]
    # The replaced copy of the same release is dropped after the good switch.
    assert forced["dropped_schemas"] == [prev]
    assert _all(db, "SELECT to_regnamespace(%s) IS NULL", (prev,)) == [(True,)]
    assert _all(
        db,
        "SELECT count(*) FROM episteme._audit WHERE event_type = 'force_override' "
        "AND reason = 'corrected snapshot'",
    ) == [(1,)]


@pytest.mark.pg
def test_forced_reload_that_fails_keeps_the_old_release_serving(db, tmp_path):
    assert _load(db, tmp_path)["status"] == "loaded"
    broken = _members(**{"conditions.txt": "id|nct_id|name|downcase_name\n"})  # no rows
    out = _load(db, tmp_path, broken, force=True, reason="test")
    assert out["status"] == "failed" and "conditions" in out["reason"]
    assert _all(db, "SELECT count(*) FROM aact.conditions") == [(2,)]
    # No good switch: the renamed copy stays, and still serves.
    assert _all(db, "SELECT to_regnamespace(%s) IS NOT NULL", (out["previous_schema"],)) == [
        (True,)
    ]


@pytest.mark.pg
def test_replaced_copies_of_other_releases_are_kept(db, tmp_path):
    """Only the _prev_ schemas of the release just switched in are dropped."""
    assert _load(db, tmp_path)["status"] == "loaded"
    with db.cursor() as cur:
        cur.execute("CREATE SCHEMA aact_r20250101_prev_20250102000000")
        cur.execute("CREATE SCHEMA aact_r20250101")
    db.commit()
    changed = _members(**{"conditions.txt": CONDITIONS + "3|NCT00000003|Gout|gout\n"})
    forced = _load(db, tmp_path, changed, force=True, reason="corrected snapshot")
    assert forced["status"] == "loaded", forced
    assert len(forced["dropped_schemas"]) == 1
    assert _all(
        db,
        "SELECT to_regnamespace('aact_r20250101_prev_20250102000000') IS NOT NULL, "
        "to_regnamespace('aact_r20250101') IS NOT NULL",
    ) == [(True, True)]


@pytest.mark.pg
def test_older_release_is_refused_unless_forced(db, tmp_path):
    assert _load(db, tmp_path, name="20261201_export_ctgov.zip")["status"] == "loaded"
    older = _load(db, tmp_path)  # 20261103
    assert older["status"] == "refused" and "older" in older["reason"], older
    assert "--force" in older["reason"]
    from episteme.data.regulatory.load_reg import latest_release

    assert latest_release(db, "aact") == "20261201"
    db.rollback()
    forced = _load(db, tmp_path, force=True, reason="roll back to the 3 Nov snapshot")
    assert forced["status"] == "loaded", forced
    assert latest_release(db, "aact") == "20261103"
    db.rollback()
    assert _all(
        db,
        "SELECT count(*) FROM episteme._audit WHERE event_type = 'force_override' "
        "AND reason = 'roll back to the 3 Nov snapshot'",
    ) == [(1,)]


@pytest.mark.pg
def test_database_error_after_the_tables_records_a_failed_release(db, tmp_path, monkeypatch):
    def boom(conn, ctx):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(la, "_resolve_interventions", boom)
    with pytest.raises(psycopg.errors.QueryCanceled):
        _load(db, tmp_path)
    assert _all(
        db, "SELECT status, is_latest FROM episteme.reg_release WHERE dataset = 'aact'"
    ) == [("failed", False)]
    assert _all(db, "SELECT to_regclass('aact.studies')") == [(None,)]


@pytest.mark.pg
def test_orphans_over_limit_keep_old_release(db, tmp_path, monkeypatch):
    assert _load(db, tmp_path)["status"] == "loaded"
    from episteme.config import get_settings

    monkeypatch.setenv("EPISTEME_AACT_MAX_ORPHAN_SHARE", "0.4")
    get_settings.cache_clear()
    orphans = _members(**{"conditions.txt": CONDITIONS + "3|NCT09999999|Gout|gout\n"})
    out = _load(db, tmp_path, orphans, name="20261201_export_ctgov.zip")
    # 1 orphan of 3 rows is under 0.4: loads.
    assert out["status"] == "loaded", out
    assert out["orphans"]["conditions"] == 1
    monkeypatch.setenv("EPISTEME_AACT_MAX_ORPHAN_SHARE", "0.05")
    get_settings.cache_clear()
    out = _load(db, tmp_path, orphans, name="20261215_export_ctgov.zip")
    assert out["status"] == "failed" and "conditions" in out["reason"], out
    from episteme.data.regulatory.load_reg import latest_release

    assert latest_release(db, "aact") == "20261201"
    db.rollback()
    assert _all(db, "SELECT count(*) FROM aact.conditions") == [(3,)]
    assert _all(db, "SELECT status FROM episteme.reg_release WHERE release_id = '20261215'") == [
        ("failed",)
    ]


@pytest.mark.pg
def test_partial_load_resumes(db, tmp_path, monkeypatch):
    calls: list[str] = []
    real = la._load_table

    failed = {"done": False}  # calls.clear() below does not reset this

    def flaky(conn, ctx, member):
        calls.append(member)
        if member == "interventions.txt" and not failed["done"]:
            failed["done"] = True
            raise RuntimeError("disk full")
        return real(conn, ctx, member)

    monkeypatch.setattr(la, "_load_table", flaky)
    with pytest.raises(RuntimeError):
        _load(db, tmp_path)
    # studies loads first, then the others in name order.
    assert calls == [
        "studies.txt",
        "brief_summaries.txt",
        "conditions.txt",
        "intervention_other_names.txt",
        "interventions.txt",
    ]
    # Nothing serves yet; the partial schema stays for the re-run.
    assert _all(db, "SELECT to_regclass('aact.studies')") == [(None,)]
    assert _all(db, "SELECT count(*) FROM aact_r20261103.studies") == [(3,)]
    calls.clear()
    out = _load(db, tmp_path)
    assert out["status"] == "loaded", out
    # The tables finished before the failure are not loaded again.
    assert calls == ["interventions.txt"]
    assert out["tables_skipped"] == 4
    assert _all(db, "SELECT count(*) FROM aact.interventions") == [(3,)]
    marker_dir = tmp_path / "proc" / "_ops" / "aact"
    assert any(marker_dir.rglob("*studies*")), "a marker per table"


@pytest.mark.pg
def test_intervention_substance_table_maps_names_to_substances(db, tmp_path):
    out = _load(db, tmp_path)
    assert out["status"] == "loaded", out
    rows = _all(
        db,
        "SELECT nct_id, intervention_id, substance_key, method FROM aact.intervention_substance "
        "ORDER BY 1, 2, 3, 4",
    )
    assert ("NCT00000001", "10", 2, "normalised_name") in rows
    assert ("NCT00000002", "11", 3, "normalised_name") in rows
    assert ("NCT00000002", "11", 3, "normalised_other_name") in rows
    assert not [r for r in rows if r[0] == "NCT00000003"]
    names = out["names"]
    assert names["matched"] == 3 and names["unmatched"] == 1
    assert _all(
        db,
        "SELECT count(*) FROM episteme.substance_xref WHERE id_type = 'aact_name' "
        "AND source = 'aact'",
    ) == [(3,)]


@pytest.mark.pg
def test_studies_equal_distinct_nct_id(db, tmp_path):
    dup = _members(**{"studies.txt": STUDIES + "NCT00000001|2020-01-01||Repeat\n"})
    out = _load(db, tmp_path, dup)
    assert out["status"] == "failed" and "distinct" in out["reason"], out
    assert _all(db, "SELECT to_regclass('aact.studies')") == [(None,)]
    assert _all(
        db, "SELECT status, is_latest FROM episteme.reg_release WHERE dataset = 'aact'"
    ) == [("failed", False)]


@pytest.mark.pg
def test_empty_table_fails(db, tmp_path):
    out = _load(db, tmp_path, _members(**{"conditions.txt": "id|nct_id|name|downcase_name\n"}))
    assert out["status"] == "failed" and "conditions" in out["reason"]


@pytest.mark.pg
def test_views_are_readable_by_the_app_role(db, tmp_path):
    assert _load(db, tmp_path)["status"] == "loaded"
    assert _all(
        db,
        "SELECT has_schema_privilege('episteme_app', 'aact', 'USAGE'), "
        "has_table_privilege('episteme_app', 'aact.studies', 'SELECT'), "
        "has_table_privilege('episteme_app', 'aact.intervention_substance', 'SELECT')",
    ) == [(True, True, True)]


@pytest.mark.pg
def test_a_dictionary_table_missing_from_the_zip_fails(db, tmp_path, monkeypatch):
    """A dictionary table missing from the zip, other than the known three, fails the load
    before any table loads; the previous release keeps serving."""
    assert _load(db, tmp_path)["status"] == "loaded"
    no_conditions = _members(**{"conditions.txt": None})
    out = _load(db, tmp_path, no_conditions, name="20261201_export_ctgov.zip")
    assert out["status"] == "failed" and "conditions" in out["reason"], out
    assert "mesh_terms" not in out["reason"].split("(")[0]
    assert _all(db, "SELECT to_regnamespace('aact_r20261201') IS NULL") == [(True,)]
    from episteme.data.regulatory.load_reg import latest_release

    assert latest_release(db, "aact") == "20261103"
    db.rollback()
    # The whole packaged dictionary: a fixture zip lacks most of its tables.
    monkeypatch.setattr(dct, "read_dictionary", _FULL_DICTIONARY)
    out = _load(db, tmp_path, name="20261215_export_ctgov.zip")
    assert out["status"] == "failed" and "outcomes" in out["reason"], out


@pytest.mark.pg
def test_a_table_without_nct_id_is_listed_and_the_gate_measures_the_orphans(db, tmp_path):
    """search_terms has no nct_id: the load summary lists it in missing_nct_id, so the
    gate's largest orphan share is a measure, not None."""
    from episteme.ops import gate_report

    members = _members(
        **{"search_terms.txt": "id|term|group|created_at|updated_at\n1|heart|causes|x|y\n"}
    )
    out = _load(db, tmp_path, members)
    assert out["status"] == "loaded", out
    assert out["missing_nct_id"] == ["search_terms"]
    summary = _all(
        db,
        "SELECT new_value FROM episteme._audit WHERE event_type = 'load_commit' "
        "AND object = 'aact 20261103' ORDER BY seq DESC LIMIT 1",
    )[0][0]
    assert summary["missing_nct_id"] == ["search_terms"]
    assert gate_report.aact_summary_measures(summary)["orphan_share_pct_max"] == 0.0


@pytest.mark.pg
def test_intervention_substance_holds_the_moiety(db, tmp_path):
    """moiety_key is substance.moiety_key, or the substance itself when that is NULL."""
    with db.cursor() as cur:
        cur.execute("UPDATE episteme.substance SET moiety_key = NULL WHERE substance_key = 3")
    db.commit()
    assert _load(db, tmp_path)["status"] == "loaded"
    rows = _all(
        db,
        "SELECT DISTINCT nct_id, substance_key, moiety_key FROM aact.intervention_substance "
        "ORDER BY 1, 2",
    )
    assert ("NCT00000001", 2, 1) in rows  # metformin hydrochloride -> metformin
    assert ("NCT00000002", 3, 3) in rows  # aspirin: no moiety_key, the substance itself
    idx = {
        r[0]
        for r in _all(
            db, "SELECT indexname FROM pg_indexes WHERE schemaname = %s", ("aact_r20261103",)
        )
    }
    assert "intervention_substance_moiety_key_idx" in idx
