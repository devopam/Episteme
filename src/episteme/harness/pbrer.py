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
from episteme.reference.lookups import FAERS_LABEL
from episteme.reference.rules import (
    MAH_MARKER,
    Finding,
    check_mah_markers,
    check_sections,
    headings,
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
GAP_PREFIX = "Coverage gap: "
NOTE_PREFIX = "Coverage note: "
# Start of every text the skeleton writes as a stand-in; ``unfilled_sections`` finds it.
SKELETON_PLACEHOLDER = "[skeleton] "
# Lookups with no interval filter: their sections say so (spec section 6).
INTERVAL_NOTES: dict[str, str] = {
    "trials": (
        "not filtered to the reporting interval; AACT lists all sponsors"
        " (E2C(R2) section 7 covers MAH trials, section 9.1 other trials)"
    ),
    "literature": "not filtered to the reporting interval",
}
# Sections whose prose the model writes from the facts above (spec section 6).
PROSE_SECTIONS = frozenset(
    {"15", "16", "16.1", "16.2", "16.3", "16.4", "16.5"}
    | {"17", "17.1", "17.2", "17.3", "18", "18.1", "18.2", "19"}
)
PROSE_BODY = SKELETON_PLACEHOLDER + "to be drafted from the facts in the sections above."
NO_SOURCE_BODY = SKELETON_PLACEHOLDER + "no public source is mapped to this section in v1."

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
    substance: LookupResult,
    start: date,
    end: date,
    *,
    tenant: str,
    caller: str,
) -> PbrerFacts:
    """Run the lookups of ``SECTION_SOURCES`` and keep one merged result per section.

    ``substance`` is the ``resolve_entity`` result; its first claim's value is the key.
    With no claim, no lookup runs and every source section states the resolution gap."""
    interval = (start, end)
    if not substance.claims or substance.claims[0].value is None:
        gap = LookupResult(
            (), Coverage(0, missing=("substance",), notes=("substance not resolved",))
        )
        return PbrerFacts(substance, interval, {sid: gap for sid in SECTION_SOURCES})
    base = {"substance_key": int(substance.claims[0].value), "tenant": tenant, "caller": caller}
    sections: dict[str, LookupResult] = {}
    for section_id, names in SECTION_SOURCES.items():
        results = []
        for name in names:
            kwargs = dict(base)
            if name in _NEEDS_DATES:
                kwargs.update(start=start, end=end)
            if name == "literature":
                kwargs["topic"] = None
            r = lookup(name, kwargs)
            if name in INTERVAL_NOTES:
                r = _merge([r, LookupResult((), Coverage(0, notes=(INTERVAL_NOTES[name],)))])
            results.append(r)
        sections[section_id] = _merge(results)
    return PbrerFacts(substance, interval, sections)


def _claim_line(c: Claim) -> str:
    p = c.provenance
    bits = [p.source]
    if p.release:
        bits.append(f"release {p.release}")
    if p.record_key:
        bits.append(p.record_key)
    return f"{c.text} [{', '.join(bits)}]"


def _gap_lines(r: LookupResult) -> list[str]:
    return [GAP_PREFIX + g for g in r.coverage.missing] + [
        NOTE_PREFIX + n for n in r.coverage.notes
    ]


def _title_page(facts: PbrerFacts) -> str:
    start, end = facts.interval
    name = facts.substance.claims[0].text if facts.substance.claims else "not resolved"
    return f"Substance: {name}\nReporting interval: {start.isoformat()} to {end.isoformat()}"


def _body(section_id: str, mah_data: bool, result: LookupResult | None) -> str:
    if mah_data:
        # Company data only: no public fact and no other text goes here.
        return MAH_MARKER
    if result is None:
        return PROSE_BODY if section_id in PROSE_SECTIONS else NO_SOURCE_BODY
    lines = [_claim_line(c) for c in result.claims] + _gap_lines(result)
    if "faers_counts" in SECTION_SOURCES.get(section_id, ()):
        lines.insert(0, FAERS_LABEL)
    return "\n".join(lines) if lines else GAP_PREFIX + "no matching records"


def skeleton(facts: PbrerFacts, *, region: Literal["ich", "eu"] = "ich") -> dict[str, str]:
    """Every E2C(R2) section of ``region`` in rule order: heading -> body."""
    rules = load_rules("ich_e2c_r2")
    return {
        s.heading: _title_page(facts)
        if s.id == "title_page"
        else _body(s.id, s.mah_data, facts.sections.get(s.id))
        for s in rules.sections_for(region)
    }


def section_headings(region: Literal["ich", "eu"] = "ich") -> list[str]:
    """The E2C(R2) section headings of ``region`` in rule order (they carry the section
    numbers a draft may cite)."""
    return headings(load_rules("ich_e2c_r2"), region)


def unfilled_sections(draft: dict[str, str]) -> list[str]:
    """Headings whose body is still skeleton placeholder text. The gate does not reject
    them in v1: not drafted is not invented."""
    return [h for h, b in draft.items() if (b or "").startswith(SKELETON_PLACEHOLDER)]


def gate_parts(
    draft: dict[str, str], *, region: Literal["ich", "eu"] = "ich"
) -> tuple[list[Finding], list[Finding]]:
    """The two parts of ``gate``: section findings (completeness, order) and MAH-marker
    findings (a mah_data section holds the marker alone, because v1 supplies no company
    data)."""
    rules = load_rules("ich_e2c_r2")
    return (
        check_sections(rules, list(draft), region=region),
        check_mah_markers(rules, draft, strict=True),
    )


def gate(draft: dict[str, str], *, region: Literal["ich", "eu"] = "ich") -> list[Finding]:
    """Rule-store checks: ``gate_parts`` joined, section findings first."""
    sections, markers = gate_parts(draft, region=region)
    return [*sections, *markers]
