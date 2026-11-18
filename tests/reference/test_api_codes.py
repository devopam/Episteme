"""lookup_code as a SQL function (migration 0015) and its Python wrapper (pg:
episteme_test only)."""

from __future__ import annotations

import dataclasses

import pytest

from episteme.reference import _api
from episteme.reference.codes import lookup_code

from . import _legacy
from ._parity import assert_same_fields
from ._pbrer_db import KW, MIGRATIONS, grant_owner

pytestmark = pytest.mark.pg

_SYSTEM = "zz_apicode"
_OID = "1.2.3.4"


@pytest.fixture
def code_db(ref_schema, pg_conn):
    """The reference schema plus migration 0013 (applied after the schema build), with
    one current and one superseded release of one system."""
    with pg_conn.cursor() as cur:
        cur.execute((MIGRATIONS / "0013_reference_codes.sql").read_text("utf-8"))
        cur.execute((MIGRATIONS / "0015_api_layer.sql").read_text("utf-8"))
    pg_conn.commit()
    grant_owner(pg_conn)  # code_current is new when 0013 was not applied before
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.code WHERE system_key = %s", (_SYSTEM,))
        cur.execute("DELETE FROM episteme.code_system WHERE system_key = %s", (_SYSTEM,))
        for rel, latest in (("r1", False), ("r2", True)):
            cur.execute(
                "INSERT INTO episteme.code_system "
                "(system_key, release_id, version, oid, licence_notice, source_file, is_latest) "
                "VALUES (%s, %s, %s, %s, 'test notice, kept with the code', 'fixture', %s)",
                (_SYSTEM, rel, "2" if rel == "r2" else None, _OID if rel == "r2" else None, latest),
            )
        rows = [
            ("r1", "JP", "Japan (old)", None),
            ("r2", "JP", "Japan", "active"),
            ("r2", "XX", "", None),
        ]
        for rel, code, display, status in rows:
            cur.execute(
                "INSERT INTO episteme.code (system_key, release_id, code, display, status, "
                "source_file) VALUES (%s, %s, %s, %s, %s, 'fixture')",
                (_SYSTEM, rel, code, display, status),
            )
    pg_conn.commit()
    yield pg_conn
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.code WHERE system_key = %s", (_SYSTEM,))
        cur.execute("DELETE FROM episteme.code_system WHERE system_key = %s", (_SYSTEM,))
    pg_conn.commit()


@pytest.mark.parametrize("code", ["JP", "XX"])
def test_code_parity(code_db, code):
    new = lookup_code(code_db, _SYSTEM, code, **KW)
    old = _legacy.lookup_code(code_db, _SYSTEM, code, **KW)
    assert len(new.claims) == 1
    claim = new.claims[0]
    # The SQL claim is the legacy claim plus the OID inside the version parentheses.
    assert claim.text.count(f", OID {_OID}): ") == 1
    old_claims = tuple(
        dataclasses.replace(c, text=c.text.replace("): ", f", OID {_OID}): ", 1))
        for c in old.claims
    )
    assert_same_fields(new, dataclasses.replace(old, claims=old_claims))
    assert "test notice, kept with the code" in claim.text
    assert claim.provenance.release == "r2"


@pytest.mark.parametrize(
    ("system", "code"), [(_SYSTEM, "NOPE"), ("zz_no_such_system", "JP"), (_SYSTEM, "jp")]
)
def test_unknown_code_note(code_db, system, code):
    """An unknown code is a note, not a `missing` row: across the API `missing` means a
    source or relation is absent, and the legacy path gave a note."""
    new = lookup_code(code_db, system, code, **KW)
    old = _legacy.lookup_code(code_db, system, code, **KW)
    assert_same_fields(new, old)
    assert new.claims == ()
    assert new.coverage.matched == 0
    assert new.coverage.notes == (f"no current code {code!r} in {system}",)


def test_public_wrapper_returns_one_claim_row(code_db):
    with code_db.cursor() as cur:
        cur.execute("SELECT * FROM episteme_api.lookup_code(%s, %s)", (_SYSTEM, "JP"))
        rows = cur.fetchall()
    result = _api.rows_to_result(rows)
    assert result.coverage.matched == 1
    assert [r[0] for r in rows] == ["claim"]
    assert rows[0][_api.CLAIM_COLUMNS.index("licence")] == "unlisted"


def test_tenant_and_caller_required(code_db):
    with pytest.raises(ValueError):
        lookup_code(code_db, _SYSTEM, "JP", tenant=" ", caller="x")


def test_code_with_quote_in_note(code_db):
    result = lookup_code(code_db, _SYSTEM, "O'Q", **KW)
    assert result.claims == ()
    assert result.coverage.notes == (f"no current code 'O'Q' in {_SYSTEM}",)


def test_missing_row_when_code_current_is_absent(code_db):
    with code_db.cursor() as cur:
        cur.execute("ALTER VIEW episteme.code_current RENAME TO code_current_hidden")
    try:
        rows = _api.call(code_db, "lookup_code", _SYSTEM, "JP")
        assert len(rows) == 1
        row = dict(zip(_api.CLAIM_COLUMNS, rows[0], strict=True))
        assert row["kind"] == "missing"
        assert row["source"] == _SYSTEM
        assert row["licence"] == "unlisted"
        assert _api.rows_to_result(rows).coverage.missing == (_SYSTEM,)
    finally:
        code_db.rollback()
