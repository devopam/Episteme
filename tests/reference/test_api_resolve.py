"""resolve_entity and substance_family as SQL functions (migration 0015), with the
results the former Python query path gave on these fixtures as fixed expectations (pg:
episteme_test only)."""

from __future__ import annotations

import os

import psycopg
import pytest

from episteme.config import LOOKUP_SOURCE_SUBSETS
from episteme.reference import _api, _scope, api_settings, resolve
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

from ._pbrer_db import KW, MIGRATIONS

pytestmark = pytest.mark.pg

_MIGRATION = MIGRATIONS / "0015_api_layer.sql"


@pytest.fixture(scope="module")
def api_schema(ref_schema):
    """Apply migration 0015 on top of the module schema (it is idempotent)."""
    conn = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(api_schema, ref_db, clean_substance):
    """The ref_db substances (1 metformin, 2 its salt, 3 aspirin), the salt also named
    'metformin' (an ambiguous name), three moieties 4-6 named 'trio' (n = 3), and the
    API settings from config."""
    api_settings.sync(ref_db)
    with ref_db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.substance_xref "
            "(substance_key, id_type, id_value, method, confidence, source) VALUES "
            "(2, 'name', 'metformin', 'normalised_name', 1.0, 'fda_substance')"
        )
        cur.execute(
            "INSERT INTO episteme.substance (substance_key, unii, preferred_name, moiety_key) "
            "VALUES (4, 'UNIITRI04', 'Trio A', 4), (5, 'UNIITRI05', 'Trio B', 5), "
            "(6, 'UNIITRI06', 'Trio C', 6)"
        )
        cur.execute(
            "INSERT INTO episteme.substance_xref "
            "(substance_key, id_type, id_value, method, confidence, source) "
            "SELECT k, 'name', 'trio', 'normalised_name', 1.0, 'fda_substance' "
            "FROM generate_series(4, 6) AS k"
        )
    ref_db.commit()
    return ref_db


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


_ONE = "exact UNII or normalised name match"
_EMPTY = LookupResult(claims=(), coverage=Coverage(matched=0, missing=("substance",)))


def _claim(key: int, unii: str, name: str, method: str, confidence: float, basis: str, alts=()):
    return Claim(
        text=name,
        value=str(key),
        method=method,
        provenance=Provenance("substance", None, None, f"unii:{unii}", None),
        confidence=confidence,
        confidence_basis=basis,
        alternatives=alts,
    )


def _one(key: int, unii: str, name: str) -> LookupResult:
    return LookupResult((_claim(key, unii, name, "fetched", 1.0, _ONE),), Coverage(matched=1))


def _mapped(*subs: tuple[int, str, str]) -> LookupResult:
    n = len(subs)
    basis = f"{n} substances share this name"
    alts = tuple(_claim(*s, "mapped", 1.0 / n, basis) for s in subs[1:])
    return LookupResult((_claim(*subs[0], "mapped", 1.0 / n, basis, alts),), Coverage(matched=n))


_METFORMIN = (1, "UNIIMET01", "Metformin")
_SALT = (2, "UNIIMET02", "Metformin Hydrochloride")
_ASPIRIN = (3, "UNIIASP03", "Aspirin")


# The results of the former Python path (pre-0015) on this fixture; the SQL path gave
# the same (parity run on episteme_test before the legacy copy was deleted).
@pytest.mark.parametrize(
    ("text", "id_type", "expected"),
    [
        ("uniiasp03", None, _one(*_ASPIRIN)),  # exact UNII
        (" Aspirin ", None, _one(*_ASPIRIN)),  # name
        ("dimethylbiguanide", None, _one(*_METFORMIN)),  # synonym
        ("metformin hydrochloride", None, _one(*_SALT)),  # salt name
        ("no such drug", None, _EMPTY),  # unknown name
        ("   ", None, _EMPTY),  # empty
        ("metformin", None, _mapped(_METFORMIN, _SALT)),  # ambiguous: moiety 1, salt 2
        # ambiguous, n = 3: confidence 1/3 exactly as the float
        (
            "trio",
            None,
            _mapped(
                (4, "UNIITRI04", "Trio A"), (5, "UNIITRI05", "Trio B"), (6, "UNIITRI06", "Trio C")
            ),
        ),
        ("aspirin", "unii", _EMPTY),  # id_type 'unii' given a name
        ("UNIIMET01", "unii", _one(*_METFORMIN)),
        ("metformin", "name", _mapped(_METFORMIN, _SALT)),
    ],
)
def test_resolve_fixed(db, text, id_type, expected):
    assert resolve.resolve_entity(db, text, **KW, id_type=id_type) == expected


def test_resolve_one_third_is_exact(db):
    (c,) = resolve.resolve_entity(db, "trio", **KW).claims
    assert c.confidence == 1 / 3
    assert [a.confidence for a in c.alternatives] == [1 / 3, 1 / 3]
    assert c.confidence_basis == "3 substances share this name"


def test_resolve_ambiguous_returns_alternatives(db):
    res = resolve.resolve_entity(db, "metformin", **KW)
    (c,) = res.claims
    assert c.value == "1"
    assert c.method == "mapped"
    assert [a.value for a in c.alternatives] == ["2"]
    assert c.confidence == 0.5
    assert c.confidence_basis == "2 substances share this name"
    assert res.coverage.matched == 2


def test_resolve_unii_arm_respects_id_type(db):
    # Changed on purpose (spec 4.3): the substance.unii arm applies only when id_type
    # is NULL or 'unii'. The former path matched a UNII under any id_type (it gave
    # substance 3 here).
    res = resolve.resolve_entity(db, "UNIIASP03", **KW, id_type="name")
    assert res.claims == ()
    assert res.coverage.missing == ("substance",)


@pytest.mark.parametrize("text", ["no such drug", "", "   ", None])
def test_resolve_unknown_is_missing_row(db, text):
    for schema in ("episteme_api_internal", "episteme_api"):
        rows = _rows(
            db,
            f"SELECT kind, claim_group, alt_rank, source, licence FROM {schema}.resolve_entity(%s)",
            (text,),
        )
        assert rows == [("missing", None, None, "substance", LOOKUP_SOURCE_SUBSETS["substance"])]
    res = resolve.resolve_entity(db, text, **KW)
    assert res.claims == ()
    assert res.coverage.matched == 0
    assert res.coverage.missing == ("substance",)


def test_resolve_nul_does_not_raise(db):
    assert resolve.resolve_entity(db, "aspi\x00rin", **KW).claims[0].value == "3"


def test_resolve_release_filled(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('fda_substance', '2026-10', 3, 'loaded', false), "
            "('fda_substance', '2026-11', 3, 'loaded', true), "
            "('ema_medicines', '2026-12', 1, 'loaded', true)"
        )
    db.commit()
    (c,) = resolve.resolve_entity(db, "aspirin", **KW).claims
    assert c.provenance.release == "2026-11"
    assert c.provenance.record_key == "unii:UNIIASP03"
    (row,) = _rows(
        db,
        "SELECT release_id, licence FROM episteme_api_internal.resolve_entity('aspirin')",
    )
    assert row == ("2026-11", LOOKUP_SOURCE_SUBSETS["substance"])


def test_resolve_release_none_without_release(db):
    (c,) = resolve.resolve_entity(db, "aspirin", **KW).claims
    assert c.provenance.release is None


_MET_NAMES = ("metformin", "metformin hydrochloride", "dimethylbiguanide")


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (1, _scope.SubstanceScope(1, 1, (1, 2), ("UNIIMET01", "UNIIMET02"), _MET_NAMES)),
        (2, _scope.SubstanceScope(2, 1, (2, 1), ("UNIIMET01", "UNIIMET02"), _MET_NAMES)),
        (3, _scope.SubstanceScope(3, 3, (3,), ("UNIIASP03",), ("aspirin",))),
        (4, _scope.SubstanceScope(4, 4, (4,), ("UNIITRI04",), ("trio a", "trio"))),
        (99, None),
    ],
)
def test_family_scope_fixed(db, key, expected):
    """The scope the former Python path gave: the input key first, the family's UNIIs,
    the preferred names (norm_name form) and then the xref names."""
    assert _scope.substance_scope(db, key) == expected


def test_family_rows(db):
    lic = LOOKUP_SOURCE_SUBSETS["substance"]
    expected = [
        (1, 1, "UNIIMET01", "Metformin", "substance", None, lic),
        (2, 1, "UNIIMET02", "Metformin Hydrochloride", "substance", None, lic),
    ]
    for schema in ("episteme_api_internal", "episteme_api"):
        assert _rows(db, f"SELECT * FROM {schema}.substance_family(2)") == expected
        assert _rows(db, f"SELECT * FROM {schema}.substance_family(3)") == [
            (3, 3, "UNIIASP03", "Aspirin", "substance", None, lic)
        ]
        assert _rows(db, f"SELECT * FROM {schema}.substance_family(99)") == []
    assert _api.call(db, "substance_family", 1) == expected


def test_family_release_filled(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('fda_substance', '2026-10', 3, 'loaded', false), "
            "('fda_substance', '2026-11', 3, 'loaded', true)"
        )
    db.commit()
    for schema in ("episteme_api_internal", "episteme_api"):
        rows = _rows(db, f"SELECT source, release_id FROM {schema}.substance_family(1)")
        assert rows == [("substance", "2026-11"), ("substance", "2026-11")]


def test_claim_row_confidence_is_double(db):
    (typ,) = _rows(
        db,
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "WHERE a.attrelid = 'episteme_api_internal.claim_row'::regclass "
        "AND a.attname = 'confidence'",
    )[0]
    assert typ == "double precision"


def test_public_resolve_caps(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.substance (substance_key, unii, preferred_name, moiety_key) "
            "SELECT k, 'CAP' || k, 'Cap ' || k, k FROM generate_series(100, 159) AS k"
        )
        cur.execute(
            "INSERT INTO episteme.substance_xref "
            "(substance_key, id_type, id_value, method, confidence, source) "
            "SELECT k, 'name', 'capdrug', 'normalised_name', 1.0, 'fda_substance' "
            "FROM generate_series(100, 159) AS k"
        )
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 50 "
            "WHERE family = 'resolve'"
        )
    db.commit()
    try:
        internal = _rows(db, "SELECT * FROM episteme_api_internal.resolve_entity('capdrug')")
        assert len(internal) == 60
        public = _rows(db, "SELECT * FROM episteme_api.resolve_entity('capdrug')")
        assert len(public) == 51
        assert public[:50] == internal[:50]
        assert [r[2] for r in public[:50]] == list(range(50))
        note = public[50]
        assert note[0] == "note"
        assert note[3] == "truncated: 50 of 60"
        mapped = _api.rows_to_result(public)
        assert mapped.coverage.notes == ("truncated: 50 of 60",)
        assert len(mapped.claims[0].alternatives) == 49
        # Under the cap: no note.
        assert [
            r[0] for r in _rows(db, "SELECT * FROM episteme_api.resolve_entity('aspirin')")
        ] == ["claim"]
    finally:
        api_settings.sync(db)


def test_cap_articles(db):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 2 "
            "WHERE family = 'literature'"
        )
    db.commit()
    try:
        rows = _rows(
            db,
            "SELECT kind, article_id, note FROM episteme_api_internal.cap_articles(ARRAY("
            "SELECT ROW(v.k, v.id, 'pubmed', NULL, NULL, NULL, NULL, NULL, NULL, v.n, NULL)"
            "::episteme_api_internal.article_row "
            "FROM (VALUES (1, 'article', 'a1', NULL), (2, 'note', NULL, 'gap'), "
            "(3, 'article', 'a2', NULL), (4, 'article', 'a3', NULL)) AS v(o, k, id, n) "
            "ORDER BY v.o), 'literature')",
        )
        assert rows == [
            ("article", "a1", None),
            ("note", None, "gap"),
            ("article", "a2", None),
            ("note", None, "truncated: 2 of 3"),
        ]
    finally:
        api_settings.sync(db)


def test_cap_precapped_note_has_no_total(db):
    """p_precapped true: the caller cut the rows already, so the note names no total."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 2 "
            "WHERE family IN ('literature', 'resolve')"
        )
    db.commit()
    try:
        arts = (
            "SELECT kind, note FROM episteme_api_internal.cap_articles(ARRAY("
            "SELECT ROW('article', 'a' || g, 'pubmed', NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, NULL)::episteme_api_internal.article_row FROM generate_series(1, 3) g), "
            "'literature'{flag})"
        )
        assert _rows(db, arts.format(flag=", true"))[-1] == ("note", "truncated: 2 of more than 2")
        assert _rows(db, arts.format(flag=""))[-1] == ("note", "truncated: 2 of 3")
        claims = (
            "SELECT kind, text FROM episteme_api_internal.cap_claims(ARRAY("
            "SELECT ROW('claim', g, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
            "NULL, NULL)::episteme_api_internal.claim_row FROM generate_series(1, 3) g), "
            "'resolve'{flag})"
        )
        assert _rows(db, claims.format(flag=", true"))[-1] == (
            "note",
            "truncated: 2 of more than 2",
        )
        assert _rows(db, claims.format(flag=", false"))[-1] == ("note", "truncated: 2 of 3")
    finally:
        api_settings.sync(db)
