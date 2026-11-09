"""Claim records returned by every reference lookup.

Stdlib only. A claim states one fact with its provenance, method and confidence.
Only a ``mapped`` claim may carry alternatives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Method = Literal["fetched", "computed", "mapped", "inferred"]
_METHODS = ("fetched", "computed", "mapped", "inferred")


@dataclass(frozen=True)
class Provenance:
    source: str
    release: str | None
    source_file: str | None
    record_key: str | None
    jurisdiction: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "release": self.release,
            "source_file": self.source_file,
            "record_key": self.record_key,
            "jurisdiction": self.jurisdiction,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Provenance:
        return cls(
            source=d["source"],
            release=d.get("release"),
            source_file=d.get("source_file"),
            record_key=d.get("record_key"),
            jurisdiction=d.get("jurisdiction"),
        )


@dataclass(frozen=True)
class Coverage:
    matched: int
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "missing": list(self.missing),
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Coverage:
        return cls(
            matched=d["matched"],
            missing=tuple(d.get("missing", ())),
            notes=tuple(d.get("notes", ())),
        )


@dataclass(frozen=True)
class Claim:
    text: str
    value: str | None
    method: Method
    provenance: Provenance
    confidence: float
    confidence_basis: str
    alternatives: tuple[Claim, ...] = ()

    def __post_init__(self) -> None:
        if self.method not in _METHODS:
            raise ValueError(f"method must be one of {_METHODS}, got {self.method!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0, 1], got {self.confidence!r}")
        if self.alternatives and self.method != "mapped":
            raise ValueError("alternatives are allowed only when method is 'mapped'")

    def to_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "value": self.value,
            "method": self.method,
            "provenance": self.provenance.to_json(),
            "confidence": self.confidence,
            "confidence_basis": self.confidence_basis,
            "alternatives": [a.to_json() for a in self.alternatives],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Claim:
        return cls(
            text=d["text"],
            value=d.get("value"),
            method=d["method"],
            provenance=Provenance.from_json(d["provenance"]),
            confidence=d["confidence"],
            confidence_basis=d["confidence_basis"],
            alternatives=tuple(cls.from_json(a) for a in d.get("alternatives", ())),
        )


@dataclass(frozen=True)
class LookupResult:
    claims: tuple[Claim, ...]
    coverage: Coverage

    def to_json(self) -> dict[str, Any]:
        return {
            "claims": [c.to_json() for c in self.claims],
            "coverage": self.coverage.to_json(),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> LookupResult:
        return cls(
            claims=tuple(Claim.from_json(c) for c in d["claims"]),
            coverage=Coverage.from_json(d["coverage"]),
        )
