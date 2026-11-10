"""resolve_entity against episteme.substance / substance_xref (pg)."""

from __future__ import annotations

import pytest

from episteme.reference.resolve import resolve_entity

pytestmark = pytest.mark.pg

KW = {"tenant": "t", "caller": "test"}

_XREF = (
    "INSERT INTO episteme.substance_xref "
    "(substance_key, id_type, id_value, method, confidence, source) VALUES "
)


@pytest.fixture
def subs(pg_conn, clean_substance):
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE episteme.substance_xref, episteme.substance RESTART IDENTITY")
        cur.execute(
            "INSERT INTO episteme.substance (substance_key, unii, preferred_name) VALUES "
            "(1, 'UNIIMET01', 'metformin'), (2, 'UNIIMET02', 'metformin hydrochloride'), "
            "(3, 'UNIIASP03', 'aspirin'), (4, 'UNIIXA004', 'xa'), (5, 'UNIIXB005', 'xb')"
        )
        cur.execute(
            "UPDATE episteme.substance SET moiety_key = CASE substance_key WHEN 2 THEN 1 "
            "ELSE substance_key END"
        )
        cur.execute(
            _XREF + "(1, 'name', 'metformin', 'normalised_name', 1.0, 'fda_substance'), "
            "(2, 'name', 'metformin hydrochloride', 'normalised_name', 1.0, 'fda_substance'), "
            "(3, 'name', 'aspirin', 'normalised_name', 1.0, 'fda_substance'), "
            "(4, 'name', 'x', 'normalised_name', 1.0, 'fda_substance'), "
            "(5, 'name', 'x', 'normalised_name', 1.0, 'fda_substance')"
        )
    pg_conn.commit()
    return pg_conn


def test_unii_resolves_exactly(subs):
    res = resolve_entity(subs, "uniiasp03", **KW)
    assert res.coverage.matched == 1
    (c,) = res.claims
    assert c.value == "3"
    assert c.text == "aspirin"
    assert c.method == "fetched"
    assert c.confidence == 1.0
    assert c.provenance.source == "substance"
    assert c.provenance.record_key == "unii:UNIIASP03"
    assert c.alternatives == ()


def test_name_resolves_exactly(subs):
    (c,) = resolve_entity(subs, " Aspirin ", **KW).claims
    assert c.value == "3"
    assert c.method == "fetched"


def test_shared_name_returns_mapped_with_alternatives(subs):
    res = resolve_entity(subs, "x", **KW)
    (c,) = res.claims
    assert c.method == "mapped"
    assert c.value == "4"  # both are moieties; lowest key wins
    assert len(c.alternatives) == 1
    assert c.alternatives[0].value == "5"
    assert c.confidence == 0.5
    assert c.confidence_basis == "2 substances share this name"
    assert res.coverage.matched == 2


def test_moiety_preferred_over_salt(subs):
    # The salt (key 2, moiety 1) also carries the name 'metformin'.
    with subs.cursor() as cur:
        cur.execute(_XREF + "(2, 'name', 'metformin', 'normalised_name', 1.0, 'fda_substance')")
    subs.commit()
    (c,) = resolve_entity(subs, "metformin", **KW).claims
    assert c.value == "1"
    assert [a.value for a in c.alternatives] == ["2"]


def test_unknown_name_returns_empty_with_coverage(subs):
    res = resolve_entity(subs, "no such drug", **KW)
    assert res.claims == ()
    assert res.coverage.matched == 0
    assert res.coverage.missing == ("substance",)


def test_empty_text_returns_empty(subs):
    res = resolve_entity(subs, "   ", **KW)
    assert res.claims == ()
    assert res.coverage.missing == ("substance",)


def test_resolve_writes_nothing(subs):
    with subs.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
    assert resolve_entity(subs, "aspirin", **KW).claims[0].value == "3"
    assert resolve_entity(subs, "x", **KW).claims[0].method == "mapped"
