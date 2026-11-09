"""PBRER v1 harness side: assemble facts, build the draft skeleton, gate the draft.

``lookup(name, kwargs)`` is injected, so the live harness and the tests share one path.
No text is invented: a section holds claim text with its provenance, or the coverage
gaps of its lookups, or a fixed marker or placeholder.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from episteme.reference.claims import Claim, Coverage, LookupResult
from episteme.reference.rules import (
    MAH_MARKER,
    Finding,
    RuleSet,
    _Resolver,
    check_mah_markers,
    check_sections,
    load_rules,
)

Lookup = Callable[[str, dict], LookupResult]

# Spec section 6, section map. Sections not listed hold no public lookup.
SECTION_SOURCES: dict[str, tuple[str, ...]] = {
    "2": ("approval_status",),
    "3": ("safety_actions",),
    "4": ("current_label",),
    "7": ("trials",),
    "9": ("faers_counts",),
    "11": ("literature",),
}
FAERS_LABEL = "US public spontaneous reports (FAERS)"
GAP_PREFIX = "Coverage gap: "
# Sections whose prose the model writes from the facts above (spec section 6).
PROSE_SECTIONS = frozenset(
    {"15", "16", "16.1", "16.2", "16.3", "16.4", "16.5"}
    | {"17", "17.1", "17.2", "17.3", "18", "18.1", "18.2", "19"}
)
PROSE_BODY = "To be drafted from the facts in the sections above."
NO_SOURCE_BODY = "No public source is mapped to this section in v1."

_NEEDS_DATES = {"safety_actions", "faers_counts"}


@dataclass(frozen=True)
class PbrerFacts:
    substance: LookupResult
    interval: tuple[date, date]
    sections: dict[str, LookupResult]


def _merge(results: list[LookupResult]) -> LookupResult:
    claims: list[Claim] = []
    missing: list[str] = []
    notes: list[str] = []
    for r in results:
        claims.extend(r.claims)
        missing.extend(m for m in r.coverage.missing if m not in missing)
        notes.extend(n for n in r.coverage.notes if n not in notes)
    return LookupResult(
        claims=tuple(claims),
        coverage=Coverage(
            matched=sum(r.coverage.matched for r in results),
            missing=tuple(missing),
            notes=tuple(notes),
        ),
    )


def assemble_facts(
    lookup: Lookup,
    substance_key: int,
    start: date,
    end: date,
    *,
    tenant: str,
    caller: str,
) -> PbrerFacts:
    """Run the lookups of ``SECTION_SOURCES`` and keep one merged result per section."""
    base = {"substance_key": substance_key, "tenant": tenant, "caller": caller}
    substance = lookup("substance", dict(base))
    sections: dict[str, LookupResult] = {}
    for section_id, names in SECTION_SOURCES.items():
        results = []
        for name in names:
            kwargs = dict(base)
            if name in _NEEDS_DATES:
                kwargs.update(start=start, end=end)
            if name == "literature":
                kwargs["topic"] = None
            results.append(lookup(name, kwargs))
        sections[section_id] = _merge(results)
    return PbrerFacts(substance=substance, interval=(start, end), sections=sections)


def _claim_line(c: Claim) -> str:
    p = c.provenance
    bits = [p.source]
    if p.release:
        bits.append(f"release {p.release}")
    if p.record_key:
        bits.append(p.record_key)
    return f"{c.text} [{', '.join(bits)}]"


def _gap_lines(r: LookupResult) -> list[str]:
    return [GAP_PREFIX + g for g in (*r.coverage.missing, *r.coverage.notes)]


def _body(section_id: str, mah_data: bool, result: LookupResult | None) -> str:
    gaps = _gap_lines(result) if result is not None else []
    if mah_data:
        # Company data only: no public fact goes here.
        return "\n".join([MAH_MARKER, *gaps])
    if result is None:
        return PROSE_BODY if section_id in PROSE_SECTIONS else NO_SOURCE_BODY
    lines = [_claim_line(c) for c in result.claims] + gaps
    if section_id == "9":
        lines.insert(0, FAERS_LABEL)
    return "\n".join(lines) if lines else GAP_PREFIX + "no matching records"


def skeleton(facts: PbrerFacts, *, region: Literal["ich", "eu"] = "ich") -> dict[str, str]:
    """Every E2C(R2) section of ``region`` in rule order: heading -> body."""
    rules = load_rules("ich_e2c_r2")
    return {
        s.heading: _body(s.id, s.mah_data, facts.sections.get(s.id))
        for s in rules.sections_for(region)
    }


def _extra_content(rules: RuleSet, draft: dict[str, str]) -> list[Finding]:
    """A mah_data body may hold the marker and skeleton gap lines, nothing else."""
    resolver = _Resolver(rules)
    findings = []
    for heading, body in draft.items():
        section, _ = resolver.resolve(heading)
        if section is None or not section.mah_data or MAH_MARKER not in (body or ""):
            continue
        rest = [ln.strip() for ln in body.replace(MAH_MARKER, "").splitlines() if ln.strip()]
        if any(not ln.startswith(GAP_PREFIX) for ln in rest):
            findings.append(
                Finding(
                    section.id,
                    section.clause,
                    f"section {section.ref} holds {MAH_MARKER!r} plus other content;"
                    " no company data is supplied in v1",
                )
            )
    return findings


def gate(draft: dict[str, str], *, region: Literal["ich", "eu"] = "ich") -> list[Finding]:
    """Rule-store checks: section completeness and order, and the MAH markers."""
    rules = load_rules("ich_e2c_r2")
    return [
        *check_sections(rules, list(draft), region=region),
        *check_mah_markers(rules, draft),
        *_extra_content(rules, draft),
    ]
