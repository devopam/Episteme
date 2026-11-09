"""Claim records: validation and JSON round trip."""

from __future__ import annotations

import pytest

from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance


def _prov(**kw) -> Provenance:
    base = dict(
        source="rxnorm",
        release="2026-11",
        source_file="rxn/RXNCONSO.RRF",
        record_key="161",
        jurisdiction=None,
    )
    base.update(kw)
    return Provenance(**base)


def _claim(**kw) -> Claim:
    base = dict(
        text="Paracetamol maps to RxCUI 161",
        value="161",
        method="fetched",
        provenance=_prov(),
        confidence=1.0,
        confidence_basis="exact source record",
    )
    base.update(kw)
    return Claim(**base)


def test_claim_confidence_must_be_in_unit_interval():
    with pytest.raises(ValueError):
        _claim(confidence=1.2)
    with pytest.raises(ValueError):
        _claim(confidence=-0.1)
    assert _claim(confidence=0.0).confidence == 0.0
    assert _claim(confidence=1.0).confidence == 1.0


def test_mapped_claim_keeps_alternatives_and_others_refuse_them():
    alt = _claim(method="mapped", confidence=0.4, confidence_basis="second match")
    with pytest.raises(ValueError):
        _claim(method="fetched", alternatives=(alt,))
    mapped = _claim(method="mapped", confidence=0.8, alternatives=(alt,))
    assert mapped.alternatives == (alt,)


def test_lookup_result_json_round_trip():
    alt = _claim(method="mapped", confidence=0.4, confidence_basis="second match")
    mapped = _claim(method="mapped", confidence=0.8, alternatives=(alt,))
    result = LookupResult(
        claims=(_claim(), mapped),
        coverage=Coverage(matched=2, missing=("x",), notes=("n1", "n2")),
    )
    data = result.to_json()
    assert isinstance(data, dict)
    again = LookupResult.from_json(data)
    assert again == result
    assert isinstance(again.claims, tuple)
    assert isinstance(again.claims[1].alternatives, tuple)
    assert isinstance(again.coverage.missing, tuple)
