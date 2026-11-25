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

from ._pbrer_db import (
    DB_DIR,
    MIGRATIONS,
    TEST_AACT_RELEASE,
    TEST_AACT_SCHEMA,
    add_release,
    grant_owner,
)

GRANTS = DB_DIR / "grants" / "0015_api_roles.sql"
TABLE_GRANTS = DB_DIR / "grants" / "0015_api_tables.sql"
AACT_GRANTS = DB_DIR / "grants" / "0015_api_aact.sql"
MIGRATION = MIGRATIONS / "0015_api_layer.sql"
SCHEMAS = ("episteme_api", "episteme_api_internal")
_SYSTEM = "zz_apirole"
_FTS_PART = "article_body_dailymed"
# A 768-dimension vector and a model revision for suggest_terms (migration 0018).
_VEC = "[" + ",".join(["0.5"] + ["0"] * 767) + "]"
_REV = "rev-test"


# ---------------------------------------------------------------------------
# non-pg: the grants file against migration 0015
# ---------------------------------------------------------------------------
def _grants_text() -> str:
    """The grants files: the roles file (superuser), the tables and AACT files
    (api_settings; the AACT loader runs only the AACT file)."""
    return "".join(p.read_text(encoding="utf-8") for p in (GRANTS, TABLE_GRANTS, AACT_GRANTS))


def _code(path) -> str:
    """A SQL file without its -- comments."""
    return "\n".join(ln.split("--", 1)[0] for ln in path.read_text(encoding="utf-8").splitlines())


def _array(name: str) -> set[str]:
    m = re.search(rf"\b{name} constant text\[\] := ARRAY\[(.*?)\];", _grants_text(), re.DOTALL)
    assert m, f"grants file has no {name} array"
    return set(re.findall(r"'([a-z_0-9]+)'", m.group(1)))


def _migration_code() -> str:
    """Migration 0015 without its -- comments."""
    return "\n".join(
        line.split("--", 1)[0] for line in MIGRATION.read_text(encoding="utf-8").splitlines()
    )


_FUNCTION = re.compile(
    r"CREATE OR REPLACE FUNCTION (episteme_api(?:_internal)?\.\w+)\((.*?)\$\$(.*?)\$\$;", re.S
)


def _functions(path) -> list[tuple[str, str, str]]:
    """(name, header, body without comments) of each function in a SQL file."""
    out = []
    for m in _FUNCTION.finditer(path.read_text(encoding="utf-8")):
        body = "\n".join(ln.split("--", 1)[0] for ln in m.group(3).splitlines())
        out.append((m.group(1), m.group(2), body))
    return out


@pytest.mark.parametrize("path", [MIGRATION, DB_DIR / "schema.sql"], ids=["0015", "schema"])
def test_every_function_that_reads_a_table_is_security_definer(path):
    """A function that reads an episteme table runs as its owner (SECURITY DEFINER), so no
    caller (the reader, episteme_app, another API function) needs a table grant."""
    functions = _functions(path)
    assert len(functions) > 30
    readers = [(n, h) for n, h, b in functions if re.search(r"\bepisteme\.[a-z]", b)]
    assert readers
    assert [n for n, h in readers if "SECURITY DEFINER" not in h] == []


@pytest.mark.parametrize("path", [MIGRATION, DB_DIR / "schema.sql"], ids=["0015", "schema"])
def test_every_function_sets_search_path_with_pg_temp_last(path):
    """Without pg_temp in the list PostgreSQL searches it FIRST for relations and types: a
    caller's temporary domain or table named text/jsonb would run inside a SECURITY
    DEFINER body (ruling R19). Every function sets search_path, ending with pg_temp."""
    text = path.read_text(encoding="utf-8")
    functions = _functions(path)
    settings = re.findall(r"^SET search_path = (.*)$", text, re.MULTILINE)
    without = [m.group(2) for m in _FUNCTION.finditer(text) if "SET search_path" not in m.group(2)]
    assert without == [], "a function without SET search_path"
    assert len(settings) >= len(functions)
    assert {v.strip() for v in settings} == {"pg_catalog, pg_temp"}


def test_owner_read_list_matches_the_migration():
    """The owner's SELECT list is every episteme relation a function body reads (the
    API migrations 0015 to 0018; 0017 reads mesh_qualifier; the 0018 functions read
    mesh_term and mesh_term_vec)."""
    code = _migration_code() + "".join(
        _code(MIGRATIONS / name)
        for name in ("0016_api_perf.sql", "0017_api_no_timeouts.sql", "0018_question_terms.sql")
    )
    read = set(re.findall(r"\b(?:FROM|JOIN)\s+episteme\.([a-z_0-9]+)", code))
    assert read, "no reads found in migration 0015"
    assert {"mesh_term", "mesh_term_vec"} <= read
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
    # norm_name: the EU label links (episteme.data.ema.epar_links) match names with it.
    assert _array("c_app") == set(_api.INTERNAL_FUNCTIONS) | {"fts_doc", "norm_name"}


def test_grants_file_shape():
    code = _code(GRANTS)
    tables = _code(TABLE_GRANTS)
    aact = _code(AACT_GRANTS)
    # The roles file is one transaction; the tables and AACT files run inside the
    # caller's one (api_settings sync, the AACT switch).
    assert code.count("BEGIN;") == 1 and code.count("COMMIT;") == 1
    for text in (tables, aact):
        assert "BEGIN;" not in text and "COMMIT;" not in text
    for text in (code, tables, aact):
        assert not re.search(r"\bPASSWORD\b", text, re.IGNORECASE), "no password in SQL"
        assert not [ln for ln in text.splitlines() if ln.lstrip().startswith("\\")]
    # The table owner runs the tables and AACT files: no role, ownership or function
    # statement.
    for text in (tables, aact):
        for word in ("CREATE ROLE", "ALTER ROLE", "OWNER TO", "DEFAULT PRIVILEGES", "PUBLIC"):
            assert word not in text, word
        assert "episteme_api_reader" not in text and "episteme_app" not in text
    assert "c_read" not in code and "c_aact" not in code
    # Ruling R21: the AACT switch runs only the AACT file. It reads episteme.reg_release
    # and grants nothing on an episteme relation, schema or routine; the tables file has
    # no AACT part.
    assert "c_aact" in aact and "c_aact" not in tables and "aact_r" not in tables
    assert set(re.findall(r"\bepisteme\.(\w+)", aact)) == {"reg_release"}
    assert "GRANT SELECT ON episteme" not in aact and "SCHEMA episteme" not in aact
    assert "ROUTINE" not in aact and "FUNCTION" not in aact
    assert "c_read" not in aact and "c_faers_acl" not in aact
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
        # MeSH terms and one vector (migration 0018): match_terms and suggest_terms.
        cur.execute(
            "INSERT INTO episteme.mesh_term (descriptor_ui, term, term_norm, term_kind, "
            "is_preferred, concept_ui, concept_relation, mesh_release, source_file) VALUES "
            "('D008107', 'Liver Diseases', 'liver diseases', 'name', true, 'M0012591', NULL, "
            "'2026', 'mesh/desc2026.gz')"
        )
        # An entry term that the dailymed label holds: search_terms finds the label.
        cur.execute(
            "INSERT INTO episteme.mesh_term (descriptor_ui, term, term_norm, term_kind, "
            "is_preferred, concept_ui, concept_relation, mesh_release, source_file) VALUES "
            "('D008107', 'Zymoquartz', 'zymoquartz', 'entry', false, 'M0012591', NULL, "
            "'2026', 'mesh/desc2026.gz')"
        )
        cur.execute(
            "INSERT INTO episteme.mesh_term_vec (descriptor_ui, text_kind, text, embedding, "
            "model, model_revision, mesh_release) VALUES "
            "('D008107', 'descriptor', 'Liver Diseases', %s::public.halfvec, 'medcpt', "
            "%s, '2026')",
            (_VEC, _REV),
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
    "match_terms": [("SELECT * FROM episteme_api.match_terms(%s)", ("liver diseases",))],
    "suggest_terms": [
        ("SELECT * FROM episteme_api.suggest_terms(%s::public.halfvec, %s)", (_VEC, _REV))
    ],
    "search_terms": [
        (
            "SELECT * FROM episteme_api.search_terms(%s::text[], %s::text[])",
            (["D008107"], ["dailymed"]),
        )
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
    grant_owner(ref_db)  # the tables file, as `api_settings sync` applies it
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
        "SELECT * FROM episteme_api_internal.match_terms('liver diseases')",
        f"SELECT * FROM episteme_api_internal.suggest_terms('{_VEC}'::public.halfvec, '{_REV}')",
        "SELECT * FROM episteme_api_internal.search_terms(ARRAY['D008107'])",
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
            # A claim, article or term row: the call read its tables, not only a 'missing'
            # path or a note.
            assert {r[0] for r in as_reader} & {"claim", "article", "term"}, (
                name,
                params,
                as_reader,
            )


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


# Temporary objects a reader may create (TEMP is granted to PUBLIC): a function that
# raises, one with a side effect, domains named like the types the bodies use, and a
# table named jsonb (its row type is pg_temp.jsonb).
_HIJACK = (
    "CREATE FUNCTION pg_temp.f(v pg_catalog.text) RETURNS pg_catalog.bool "
    "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'hijacked'; END $$",
    "CREATE TABLE pg_temp.hits (v pg_catalog.text)",
    "CREATE FUNCTION pg_temp.g(v pg_catalog.text) RETURNS pg_catalog.bool "
    "LANGUAGE plpgsql AS $$ BEGIN INSERT INTO pg_temp.hits VALUES (v); RETURN true; END $$",
    "CREATE DOMAIN pg_temp.text AS pg_catalog.text CHECK (pg_temp.f(VALUE))",
    "CREATE DOMAIN pg_temp.date AS pg_catalog.date CHECK (pg_temp.g(VALUE::pg_catalog.text))",
    "CREATE DOMAIN pg_temp.int4 AS pg_catalog.int4 CHECK (pg_temp.g(VALUE::pg_catalog.text))",
    "CREATE DOMAIN pg_temp.int8 AS pg_catalog.int8 CHECK (pg_temp.g(VALUE::pg_catalog.text))",
    "CREATE DOMAIN pg_temp.bool AS pg_catalog.bool CHECK (pg_temp.g(VALUE::pg_catalog.text))",
    "CREATE TABLE pg_temp.jsonb (x pg_catalog.int4)",
)


@pytest.mark.pg
def test_temp_object_hijack_is_harmless(api):
    """R19: with pg_temp LAST in every function's search_path, the reader's temporary
    domains, functions and tables never run inside a SECURITY DEFINER body. Every public
    function gives the clean answer, nothing raises 'hijacked', and no side-effect row is
    written."""
    clean = {
        (name, i): _rows(api.admin, query, params)
        for name, calls in CALLS.items()
        for i, (query, params) in enumerate(calls)
    }
    reader = api.reader()
    with reader.cursor() as cur:
        for statement in _HIJACK:
            cur.execute(statement)
        for name, calls in CALLS.items():
            for i, (query, params) in enumerate(calls):
                # The caller's own casts are qualified: the reader's session resolves
                # 'text' to its domain, which is the reader's business, not the API's.
                cur.execute(query.replace("::text[]", "::pg_catalog.text[]"), params)
                assert cur.fetchall() == clean[(name, i)], (name, params)
        cur.execute("SELECT pg_catalog.count(*) FROM pg_temp.hits")
        assert cur.fetchone()[0] == 0
    reader.rollback()


@pytest.mark.pg
def test_settings_tables_are_read_only_for_the_definer(api):
    """R19: the settings tables belong to episteme_sys_admin (api_settings sync writes
    them); the owner role, the identity of every SECURITY DEFINER body, may only read."""
    rows = _rows(
        api.admin,
        "SELECT c.relname, c.relowner::regrole::text, "
        "has_table_privilege('episteme_api_owner', c.oid, 'SELECT'), "
        "has_table_privilege('episteme_api_owner', c.oid, "
        "'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') "
        "FROM pg_class c WHERE c.oid IN ('episteme_api_internal.source_subset'::regclass, "
        "'episteme_api_internal.result_limit'::regclass) ORDER BY 1",
    )
    assert rows == [
        ("result_limit", "episteme_sys_admin", True, False),
        ("source_subset", "episteme_sys_admin", True, False),
    ]
    # The sync still writes them, as episteme_sys_admin.
    assert api_settings.sync(api.admin)["result_limit"] > 0


@pytest.mark.pg
def test_pipeline_role_calls_the_internal_functions_and_writes_indexed_bodies(api):
    """episteme_app (the pipeline and the Python lookups) executes the 12 complete
    functions of _api.INTERNAL_FUNCTIONS, and fts_doc when it writes into a partition
    with a full-text index. Rolled back."""
    with psycopg.connect(api.su_dsn, autocommit=False) as su:
        if su.execute("SELECT to_regrole('episteme_app') IS NULL").fetchone()[0]:
            pytest.skip("role episteme_app does not exist")
        su.execute("SET ROLE episteme_app")
        # match_terms, suggest_terms and search_terms (0018) are for the reader (the
        # question MCP), not for the pipeline role.
        assert set(_api.INTERNAL_FUNCTIONS) <= set(CALLS)
        for name in sorted(_api.INTERNAL_FUNCTIONS):
            query, params = CALLS[name][0]
            rows = su.execute(
                query.replace("episteme_api.", "episteme_api_internal."), params
            ).fetchall()
            assert rows, name
        su.execute(
            "INSERT INTO episteme.article_body (article_id, source, title, body_text) "
            "VALUES ('dailymed:app1', 'dailymed', 'App label', 'written by the pipeline')"
        )
        assert (
            su.execute(
                "SELECT pg_catalog.count(*) FROM pg_catalog.pg_index i "
                "WHERE i.indrelid = 'episteme.article_body_dailymed'::regclass "
                "AND i.indexrelid = %s::regclass",
                (f"episteme.{fts_index.index_name(_FTS_PART)}",),
            ).fetchone()[0]
            == 1
        )
        su.rollback()


@pytest.mark.pg
def test_internal_functions_are_safe_to_call(api):
    """In the database: every API function that reads an episteme table is SECURITY
    DEFINER, and the owner role can execute every API function (it calls the helpers as
    itself)."""
    rows = _rows(
        api.admin,
        "SELECT p.oid::regprocedure::text, p.prosecdef, "
        r"p.prosrc ~ '\mepisteme\.[a-z]', "
        "has_function_privilege('episteme_api_owner', p.oid, 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname IN ('episteme_api', 'episteme_api_internal')",
    )
    assert rows
    assert [sig for sig, definer, reads, _ in rows if reads and not definer] == []
    assert [sig for sig, _, _, owner_exec in rows if not owner_exec] == []


@pytest.mark.pg
def test_a_helper_recreated_by_a_migration_stays_callable(api):
    """A migration that drops and creates a helper leaves it owned by episteme_sys_admin
    until the grants file runs again. The default privileges let the owner role call it,
    so the functions it owns keep working (round-2 failure: "permission denied for
    function substance_family"). Rolled back."""
    create = next(
        m.group(0)
        for m in _FUNCTION.finditer(MIGRATION.read_text(encoding="utf-8"))
        if m.group(1) == "episteme_api_internal.substance_family"
    )
    conn = api.admin
    with conn.cursor() as cur:
        cur.execute("DROP FUNCTION episteme_api_internal.substance_family(bigint)")
        cur.execute(create)
        cur.execute(
            "SELECT proowner::regrole::text FROM pg_proc "
            "WHERE oid = 'episteme_api_internal.substance_family(bigint)'::regprocedure"
        )
        assert cur.fetchone()[0] != "episteme_api_owner"
        cur.execute("SELECT kind FROM episteme_api.trials(1)")
        assert "claim" in {r[0] for r in cur.fetchall()}
    conn.rollback()


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
