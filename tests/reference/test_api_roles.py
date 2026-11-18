"""Roles and grants of the SQL lookup API (grants/0015_api_roles.sql): the source lists
match migration 0015 (non-pg), and the reader role cannot escape (pg: episteme_test only).

The pg tests apply the grants file as a superuser (POSTGRES_SUPERUSER, default postgres,
and POSTGRES_SUPERUSER_PASSWORD; skipped without them unless TEST_PG_DSN is a superuser).
Roles are global to the server, so the tests never change episteme_api_reader itself: each
test creates a throwaway LOGIN role with a random password, makes it a member of
episteme_api_reader, and drops it at the end. The file lives here, not in tests/data, to
use the ``ref_db`` fixture (as test_pbrer_cases_pg does).
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from episteme.data.db import fts_index
from episteme.data.regulatory.faers.generation import PARENTS, SIGNAL_TABLES
from episteme.reference import _api, api_settings

from ._pbrer_db import DB_DIR, MIGRATIONS, TEST_AACT_RELEASE, TEST_AACT_SCHEMA, add_release

GRANTS = DB_DIR / "grants" / "0015_api_roles.sql"
MIGRATION = MIGRATIONS / "0015_api_layer.sql"
SCHEMAS = ("episteme_api", "episteme_api_internal")
_SYSTEM = "zz_apirole"
_FTS_PART = "article_body_dailymed"


# ---------------------------------------------------------------------------
# non-pg: the grants file against migration 0015
# ---------------------------------------------------------------------------
def _grants_text() -> str:
    return GRANTS.read_text(encoding="utf-8")


def _array(name: str) -> set[str]:
    m = re.search(rf"\b{name} constant text\[\] := ARRAY\[(.*?)\];", _grants_text(), re.DOTALL)
    assert m, f"grants file has no {name} array"
    return set(re.findall(r"'([a-z_0-9]+)'", m.group(1)))


def _migration_code() -> str:
    """Migration 0015 without its -- comments."""
    return "\n".join(
        line.split("--", 1)[0] for line in MIGRATION.read_text(encoding="utf-8").splitlines()
    )


def test_owner_read_list_matches_the_migration():
    """The owner's SELECT list is every episteme relation a function body reads."""
    read = set(re.findall(r"\b(?:FROM|JOIN)\s+episteme\.([a-z_0-9]+)", _migration_code()))
    assert read, "no reads found in migration 0015"
    assert _array("c_read") == read


def test_aact_list_matches_the_migration():
    read = set(re.findall(r"\b(?:FROM|JOIN)\s+%I\.([a-z_0-9]+)", _migration_code()))
    assert (
        _array("c_aact")
        == read
        == {
            "studies",
            "intervention_substance",
            "study_references",
            "_load_state",
        }
    )


def test_faers_acl_list_is_faers_report_and_the_signal_tables():
    """generation._check_access compares every signal table with faers_report, so the
    owner's grant covers all of them; a read FAERS table that is swapped is in the list."""
    acl = _array("c_faers_acl")
    assert acl == {PARENTS[0], *SIGNAL_TABLES}
    swapped = {*PARENTS, *SIGNAL_TABLES}
    assert {t for t in _array("c_read") if t in swapped} <= acl


def test_app_functions_are_the_python_entry_points_and_the_index_expression():
    assert _array("c_app") == set(_api.INTERNAL_FUNCTIONS) | {"fts_doc"}


def test_grants_file_shape():
    text = _grants_text()
    code = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
    assert code.count("BEGIN;") == 1 and code.count("COMMIT;") == 1
    assert not re.search(r"\bPASSWORD\b", code, re.IGNORECASE), "no password in SQL"
    assert not [ln for ln in code.splitlines() if ln.lstrip().startswith("\\")]
    assert (
        "REVOKE ALL ON ALL ROUTINES IN SCHEMA episteme_api, episteme_api_internal FROM PUBLIC"
        in code
    )
    assert "GRANT EXECUTE ON ALL ROUTINES IN SCHEMA episteme_api TO episteme_api_reader" in code
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE episteme_sys_admin" in code


# ---------------------------------------------------------------------------
# pg
# ---------------------------------------------------------------------------
def _dsn_parts() -> dict:
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    parts = conninfo_to_dict(dsn)
    if parts.get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to change grants")
    return parts


def _su_dsn() -> str:
    parts = _dsn_parts()
    with psycopg.connect(make_conninfo(**parts)) as conn:
        if conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
            return make_conninfo(**parts)
    pw = os.environ.get("POSTGRES_SUPERUSER_PASSWORD")
    if not pw:
        pytest.skip("POSTGRES_SUPERUSER_PASSWORD not set: the grants file needs a superuser")
    parts["user"] = os.environ.get("POSTGRES_SUPERUSER") or "postgres"
    parts["password"] = pw
    return make_conninfo(**parts)


@pytest.fixture(scope="module")
def fts(ref_schema):
    """The full-text index of the dailymed partition (search_text skips a partition
    without one)."""
    conn = psycopg.connect(make_conninfo(**_dsn_parts()), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(MIGRATION.read_text(encoding="utf-8"))
            cur.execute(fts_index.build_sql(_FTS_PART))
        yield
        with conn.cursor() as cur:
            cur.execute(
                f"DROP INDEX CONCURRENTLY IF EXISTS episteme.{fts_index.index_name(_FTS_PART)}"
            )
    finally:
        conn.close()


def _seed(conn) -> None:
    """One row path for every public function (ref_db holds substances 1 metformin,
    2 its salt, 3 aspirin)."""
    add_release(
        conn,
        "fda_drugsfda",
        "r1",
        [{"record_key": "NDA-1", "substance_names": ["metformin"], "payload": {}}],
        latest=True,
    )
    add_release(
        conn,
        "fda_enforcement",
        "r1",
        [
            {
                "record_key": "REC-1",
                "substance_names": ["metformin"],
                "event_date": date(2024, 3, 1),
                "payload": {},
            }
        ],
        latest=True,
    )
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.article_body WHERE source = 'dailymed'")
        # Labels: set1 lists the salt UNII; its text is in article_body.
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, year, book_meta, pmid) VALUES "
            "('dailymed:set1', 'dailymed', 'dailymed/p1.parquet', 2024, %s::jsonb, NULL), "
            "('pubmed:100', 'pubmed', 'pubmed/a.xml', 2020, NULL, '100'), "
            "('pubmed:200', 'pubmed', 'pubmed/b.xml', 2021, NULL, '200')",
            (json.dumps({"version": "7"}),),
        )
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, title, body_text) VALUES "
            "('dailymed:set1', 'dailymed', 'METFORMIN label', "
            "'ADVERSE REACTIONS zymoquartz nausea')"
        )
        cur.execute(
            "INSERT INTO episteme.label_substance (article_id, unii, ndc_product) VALUES "
            "('dailymed:set1', 'UNIIMET02', '000000001')"
        )
        cur.execute(
            "INSERT INTO episteme.label_section "
            "(article_id, loinc_code, title, start_offset, end_offset) VALUES "
            "('dailymed:set1', '34084-4', 'ADVERSE REACTIONS', 0, 17)"
        )
        # FAERS
        cur.execute(
            "INSERT INTO episteme.faers_moiety_quarter "
            "(substance_key, quarter, reports_suspect, reports_any) VALUES (1, '2025-01-01', 5, 8)"
        )
        cur.execute(
            "INSERT INTO episteme.faers_quarter_totals (quarter, reports) "
            "VALUES ('2025-01-01', 1000)"
        )
        cur.execute(
            "INSERT INTO episteme.faers_moiety_quarter_outcome "
            "(substance_key, quarter, seriousness, outcome, reports) "
            "VALUES (1, '2025-01-01', 'serious', 'HO', 3)"
        )
        cur.execute(
            "INSERT INTO episteme.faers_term (term, term_norm) VALUES ('Nausea', 'nausea') "
            "RETURNING term_id"
        )
        term = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO episteme.faers_event_quarter "
            "(substance_key, term_id, quarter, reports_suspect, reports_any) "
            "VALUES (1, %s, '2025-01-01', 2, 3)",
            (term,),
        )
        # Literature and graph
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('pubmed', '2026', 2, 'loaded', true)"
        )
        cur.execute(
            "INSERT INTO episteme.mesh_tree (descriptor_ui, tree_number) "
            "VALUES ('D008107', 'C06.552')"
        )
        cur.execute(
            "INSERT INTO episteme.article_mesh "
            "(pmid, descriptor_ui, descriptor_name, major_topic, qualifiers, source_file) VALUES "
            "('100', 'D008687', 'Metformin', true, NULL, 'pubmed/a.xml'), "
            "('100', 'D008107', 'Liver Diseases', true, NULL, 'pubmed/a.xml'), "
            "('200', 'D008107', 'Liver Diseases', true, NULL, 'pubmed/b.xml')"
        )
        cur.execute(
            "INSERT INTO episteme.article_cites (src_pmid, dst_pmid, source_file) "
            "VALUES ('100', '200', 'cites/a.xml')"
        )
        cur.execute(
            "INSERT INTO episteme.article_chemicals (pmid, name, source_file) "
            "VALUES ('100', 'Metformin', 'pubmed/a.xml')"
        )
        # Codes (migration 0013)
        cur.execute("DELETE FROM episteme.code WHERE system_key = %s", (_SYSTEM,))
        cur.execute("DELETE FROM episteme.code_system WHERE system_key = %s", (_SYSTEM,))
        cur.execute(
            "INSERT INTO episteme.code_system "
            "(system_key, release_id, version, oid, licence_notice, source_file, is_latest) "
            "VALUES (%s, 'r1', '1', NULL, 'notice', 'fixture', true)",
            (_SYSTEM,),
        )
        cur.execute(
            "INSERT INTO episteme.code (system_key, release_id, code, display, status, "
            "source_file) VALUES (%s, 'r1', 'JP', 'Japan', 'active', 'fixture')",
            (_SYSTEM,),
        )
        # AACT release schema
        s = sql.Identifier(TEST_AACT_SCHEMA)
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(s))
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.studies (nct_id text, brief_title text, overall_status text, "
                "phase text, results_first_posted_date text)"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.intervention_substance (nct_id text, intervention_id text, "
                "substance_key bigint, moiety_key bigint, method text)"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.study_references (id text, nct_id text, pmid text, "
                "reference_type text, citation text)"
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
                "INSERT INTO {}.studies VALUES ('NCT01', 'Metformin in X', 'COMPLETED', "
                "'PHASE3', NULL)"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.intervention_substance VALUES "
                "('NCT01', 'i1', 1, 1, 'normalised_name')"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.study_references VALUES ('1', 'NCT01', '100', 'RESULT', 'c')"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}._load_state (table_name, fingerprint, source_file, rows, stats) "
                "VALUES ('_release', 'fp', 'aact/x.zip', 1, '{{}}'::jsonb)"
            ).format(s)
        )
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('aact', %s, 1, 'loaded', true)",
            (TEST_AACT_RELEASE,),
        )
    conn.commit()


# One or more calls per public function; the keys must be every function of episteme_api.
CALLS: dict[str, list[tuple[str, tuple]]] = {
    "resolve_entity": [("SELECT * FROM episteme_api.resolve_entity(%s)", ("metformin",))],
    "substance_family": [("SELECT * FROM episteme_api.substance_family(%s)", (1,))],
    "approval_status": [("SELECT * FROM episteme_api.approval_status(%s)", (1,))],
    "safety_actions": [
        (
            "SELECT * FROM episteme_api.safety_actions(%s, %s, %s)",
            (1, date(2024, 1, 1), date(2024, 12, 31)),
        )
    ],
    "current_label": [("SELECT * FROM episteme_api.current_label(%s)", (2,))],
    "label_section_text": [
        ("SELECT * FROM episteme_api.label_section_text(%s, %s)", ("dailymed:set1", "34084-4"))
    ],
    "trials": [("SELECT * FROM episteme_api.trials(%s)", (1,))],
    "faers_counts": [
        (
            "SELECT * FROM episteme_api.faers_counts(%s, %s, %s)",
            (1, date(2025, 1, 1), date(2025, 3, 31)),
        )
    ],
    "literature_by_mesh": [
        ("SELECT * FROM episteme_api.literature_by_mesh(%s, %s)", (1, None)),
        ("SELECT * FROM episteme_api.literature_by_mesh(%s, %s)", (1, "Liver Diseases")),
    ],
    "graph_neighbours": [
        ("SELECT * FROM episteme_api.graph_neighbours(%s, %s)", ("pubmed:100", kind))
        for kind in ("cites", "trial", "label", "shared_mesh")
    ],
    "lookup_code": [("SELECT * FROM episteme_api.lookup_code(%s, %s)", (_SYSTEM, "JP"))],
    "search_text": [
        ("SELECT * FROM episteme_api.search_text(%s, %s::text[])", ("zymoquartz", ["dailymed"]))
    ],
}


class Api:
    def __init__(self, admin, su_dsn: str, role: str, password: str):
        self.admin = admin
        self.su_dsn = su_dsn
        self.role = role
        self.password = password
        self._conns: list[psycopg.Connection] = []

    def reader(self) -> psycopg.Connection:
        parts = _dsn_parts()
        parts.update(user=self.role, password=self.password)
        conn = psycopg.connect(make_conninfo(**parts), autocommit=False)
        self._conns.append(conn)
        return conn

    def close(self) -> None:
        for conn in self._conns:
            conn.close()


def _apply_grants(su_dsn: str) -> None:
    with psycopg.connect(su_dsn, autocommit=False) as su:
        su.execute(GRANTS.read_text(encoding="utf-8"))
        su.commit()


@pytest.fixture
def api(fts, ref_db):
    """The seeded schema with the grants file applied, and a throwaway reader."""
    with ref_db.cursor() as cur:
        cur.execute((MIGRATIONS / "0013_reference_codes.sql").read_text("utf-8"))
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    ref_db.commit()
    api_settings.sync(ref_db)
    _seed(ref_db)
    su_dsn = _su_dsn()
    _apply_grants(su_dsn)
    role = f"episteme_api_reader_t_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    with psycopg.connect(su_dsn, autocommit=True) as su:
        su.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        su.execute(sql.SQL("GRANT episteme_api_reader TO {}").format(sql.Identifier(role)))
    out = Api(ref_db, su_dsn, role, password)
    try:
        yield out
    finally:
        out.close()
        ref_db.rollback()
        with psycopg.connect(su_dsn, autocommit=True) as su:
            su.execute("DROP SCHEMA IF EXISTS api_role_game CASCADE")
            su.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            su.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        with ref_db.cursor() as cur:
            cur.execute("DELETE FROM episteme.article_body WHERE source = 'dailymed'")
            cur.execute("DELETE FROM episteme.code WHERE system_key = %s", (_SYSTEM,))
            cur.execute("DELETE FROM episteme.code_system WHERE system_key = %s", (_SYSTEM,))
        ref_db.commit()


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    conn.rollback()
    return rows


@pytest.mark.pg
def test_reader_cannot_select_base_table(api):
    rows = _rows(
        api.admin,
        "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname IN "
        "('episteme', 'episteme_api', 'episteme_api_internal', %s) "
        "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "AND has_table_privilege(%s, c.oid, 'SELECT')",
        (TEST_AACT_SCHEMA, api.role),
    )
    assert rows == []
    for schema in ("episteme", "episteme_api_internal", TEST_AACT_SCHEMA):
        assert _rows(
            api.admin, "SELECT has_schema_privilege(%s, %s, 'USAGE')", (api.role, schema)
        ) == [(False,)], schema
    reader = api.reader()
    for query in (
        "SELECT 1 FROM episteme.substance LIMIT 1",
        "SELECT 1 FROM episteme_api_internal.result_limit LIMIT 1",
        f"SELECT 1 FROM {TEST_AACT_SCHEMA}.studies LIMIT 1",
    ):
        with pytest.raises(errors.InsufficientPrivilege):
            reader.execute(query)
        reader.rollback()


@pytest.mark.pg
def test_reader_cannot_execute_internal(api):
    rows = _rows(
        api.admin,
        "SELECT p.oid::regprocedure::text, n.nspname, p.proowner::regrole::text, "
        "has_function_privilege(%s, p.oid, 'EXECUTE'), "
        "EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "        WHERE a.grantee = 0) "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname IN ('episteme_api', 'episteme_api_internal')",
        (api.role,),
    )
    assert rows
    for sig, schema, owner, reader_exec, public_exec in rows:
        assert owner == "episteme_api_owner", sig
        assert not public_exec, f"{sig} is executable by PUBLIC"
        assert reader_exec == (schema == "episteme_api"), sig
    # No function in episteme or the AACT schema is executable by the reader.
    assert (
        _rows(
            api.admin,
            "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname IN ('episteme', %s) AND has_function_privilege(%s, p.oid, 'EXECUTE')",
            (TEST_AACT_SCHEMA, api.role),
        )
        == []
    )
    reader = api.reader()
    for query in (
        "SELECT * FROM episteme_api_internal.resolve_entity('metformin')",
        "SELECT episteme_api_internal.limit_for('search')",
    ):
        with pytest.raises(errors.InsufficientPrivilege):
            reader.execute(query)
        reader.rollback()


@pytest.mark.pg
def test_owner_reads_exactly_the_listed_relations(api):
    """The owner's direct SELECT in episteme and the AACT schema: the grants file lists
    (c_read, c_faers_acl, c_aact), nothing else."""
    rows = _rows(
        api.admin,
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname IN ('episteme', %s) "
        "AND has_table_privilege('episteme_api_owner', c.oid, 'SELECT')",
        (TEST_AACT_SCHEMA,),
    )
    episteme = {r for s, r in rows if s == "episteme"}
    aact = {r for s, r in rows if s == TEST_AACT_SCHEMA}
    expected = _array("c_read") | _array("c_faers_acl")
    existing = {
        r[0]
        for r in _rows(
            api.admin, "SELECT relname FROM pg_class WHERE relnamespace = 'episteme'::regnamespace"
        )
    }
    assert episteme == expected & existing
    assert aact == _array("c_aact")


@pytest.mark.pg
def test_reader_can_call_each_public_function(api):
    names = {
        r[0]
        for r in _rows(
            api.admin,
            "SELECT p.proname FROM pg_proc p WHERE p.pronamespace = 'episteme_api'::regnamespace",
        )
    }
    assert names == set(CALLS), "every public function needs a call in CALLS"
    reader = api.reader()
    for name, calls in CALLS.items():
        for query, params in calls:
            as_reader = _rows(reader, query, params)
            assert as_reader == _rows(api.admin, query, params), (name, params)
            assert as_reader, (name, params)
            if name == "substance_family":
                continue
            # A claim or article row: the call read its tables, not only a 'missing' path.
            assert {r[0] for r in as_reader} & {"claim", "article"}, (name, params, as_reader)


@pytest.mark.pg
def test_public_function_in_read_only_transaction(api):
    reader = api.reader()
    with reader.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT * FROM episteme_api.resolve_entity('metformin')")
        assert any(r[0] == "claim" for r in cur.fetchall())
        cur.execute("SELECT * FROM episteme_api.search_text('zymoquartz', ARRAY['dailymed'])")
        assert any(r[0] == "article" for r in cur.fetchall())
    reader.rollback()


@pytest.mark.pg
def test_search_path_game_harmless(api):
    """A schema and a temporary table named like the base tables, first on the reader's
    search_path, change nothing: every function body is schema-qualified with
    search_path pg_catalog."""
    query = "SELECT * FROM episteme_api.resolve_entity('metformin')"
    baseline = _rows(api.admin, query)
    with psycopg.connect(api.su_dsn, autocommit=True) as su:
        su.execute("CREATE SCHEMA api_role_game")
        su.execute(
            "CREATE TABLE api_role_game.substance (substance_key bigint, unii text, "
            "preferred_name text, moiety_key bigint)"
        )
        su.execute("INSERT INTO api_role_game.substance VALUES (99, 'FAKE', 'metformin', 99)")
        su.execute(
            "CREATE FUNCTION api_role_game.lower(text) RETURNS text LANGUAGE sql "
            "AS $$ SELECT 'fake' $$"
        )
        su.execute(
            sql.SQL("GRANT USAGE ON SCHEMA api_role_game TO {}").format(sql.Identifier(api.role))
        )
        su.execute(
            sql.SQL("GRANT SELECT ON api_role_game.substance TO {}").format(
                sql.Identifier(api.role)
            )
        )
    reader = api.reader()
    with reader.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE substance (substance_key bigint, unii text, "
            "preferred_name text, moiety_key bigint)"
        )
        cur.execute("INSERT INTO substance VALUES (98, 'TEMP', 'metformin', 98)")
        cur.execute("SET search_path = api_role_game, pg_temp, public")
        cur.execute(query)
        assert cur.fetchall() == baseline
        cur.execute("SET search_path = pg_temp, api_role_game")
        cur.execute(query)
        assert cur.fetchall() == baseline
    reader.rollback()


@pytest.mark.pg
def test_reader_role_settings(api):
    """The grants file's role shape, and the statement_timeout that api_settings sets."""
    rows = _rows(
        api.admin,
        "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
        "FROM pg_roles WHERE rolname IN ('episteme_api_owner', 'episteme_api_reader') "
        "ORDER BY 1",
    )
    assert rows == [
        ("episteme_api_owner", False, False, False, False, False),
        ("episteme_api_reader", True, False, False, False, False),
    ]
    # episteme_sys_admin (the test connection) may set the reader's timeout.
    # An open transaction first, so that the function's transaction() is a savepoint: the
    # ALTER ROLE (global to the server) is rolled back by _rows after the read.
    api.admin.execute("SELECT 1")
    assert api_settings.apply_reader_settings(api.admin, 30) == "30s"
    setting = _rows(
        api.admin,
        "SELECT s.setconfig FROM pg_db_role_setting s "
        "WHERE s.setrole = 'episteme_api_reader'::regrole AND s.setdatabase = 0",
    )
    assert setting and "statement_timeout=30s" in setting[0][0]
