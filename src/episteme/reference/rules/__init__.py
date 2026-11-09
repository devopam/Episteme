"""Versioned rule store: implementation guides and guidelines held as packaged JSON.

Each rule file (``data/<ig_id>.json``) names its IG, version and source documents with
their sha256, and lists checkable items. Every item keeps the clause of its source.

Section checks (``check_sections``, ``check_mah_markers``) match draft headings to rule
sections: case-insensitive, punctuation-insensitive, leading section number optional.
Both checks use one resolver, so a caller can pass ``list(draft)`` and ``draft``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Literal

MAH_MARKER = "MAH data required"

_IG_ID = re.compile(r"^[a-z0-9_]+$")
_REGIONS: dict[str, frozenset[str]] = {
    "ich": frozenset({"ich"}),
    "eu": frozenset({"ich", "eu"}),
}
_DOC_BY_REGION = {"ich": "ICH E2C(R2)", "eu": "EU GVP Module VII"}
# A leading "Appendix 2", "5.1", "5.1." or "Section 5.1" before the title.
_NUMBER = re.compile(
    r"^\s*(?:section\s+)?(?P<num>appendix\s+\d+(?:\.\d+)*|\d+(?:\.\d+)*)\.?(?=\s|$)\s*[-–—:.)]*\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Section:
    id: str
    number: str | None
    title: str
    required: bool
    mah_data: bool
    clause: str
    region: str
    note: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def heading(self) -> str:
        """The canonical heading: ``"5.1. Title"``, ``"Appendix 1. Title"`` or ``"Title"``."""
        return f"{self.number}. {self.title}" if self.number else self.title

    @property
    def ref(self) -> str:
        """The section and its clause, for messages."""
        doc = _DOC_BY_REGION.get(self.region, self.region)
        return f"{self.heading!r} ({doc} {self.clause})"


@dataclass(frozen=True)
class RuleSet:
    ig_id: str
    ig: str
    version: str
    source_docs: tuple[dict[str, str], ...]
    sections: tuple[Section, ...] = ()
    data: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def sections_for(self, region: str) -> tuple[Section, ...]:
        allowed = _allowed(region)
        return tuple(s for s in self.sections if s.region in allowed)


@dataclass(frozen=True)
class Finding:
    rule_id: str
    clause: str
    message: str


def _allowed(region: str) -> frozenset[str]:
    try:
        return _REGIONS[region]
    except KeyError:
        raise ValueError(f"unknown region {region!r}; expected one of {sorted(_REGIONS)}") from None


def load_rules(ig_id: str) -> RuleSet:
    """Load the packaged rule file ``data/<ig_id>.json``."""
    if not _IG_ID.match(ig_id):
        raise ValueError(f"invalid rule set id {ig_id!r}")
    path = resources.files(__name__) / "data" / f"{ig_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no rule file for {ig_id!r} (expected data/{ig_id}.json)")
    data = json.loads(path.read_text(encoding="utf-8"))
    sections = tuple(
        Section(
            id=str(s["id"]),
            number=s.get("number"),
            title=s["title"],
            required=bool(s["required"]),
            mah_data=bool(s["mah_data"]),
            clause=str(s["clause"]),
            region=s["region"],
            note=s.get("note"),
            aliases=tuple(s.get("aliases", ())),
        )
        for s in data.get("sections", ())
    )
    return RuleSet(
        ig_id=ig_id,
        ig=data["ig"],
        version=data["version"],
        source_docs=tuple(data["source_docs"]),
        sections=sections,
        data=data,
    )


def _norm(text: str) -> str:
    """Casefold and keep only letters and digits, single-space separated."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.casefold()).split())


def _split_number(heading: str) -> tuple[str | None, str]:
    m = _NUMBER.match(heading)
    if not m or not heading[m.end() :].strip():
        return None, _norm(heading)
    return _norm(m.group("num")), _norm(heading[m.end() :])


class _Resolver:
    """Map headings to sections in one pass. A title shared by two sections (for
    example 6.1 and Appendix 1 "Reference Information") resolves by its number when
    the heading has one, else to the next unmatched section in rule order."""

    def __init__(self, rules: RuleSet) -> None:
        self._rules = rules
        self._by_title: dict[str, list[int]] = {}
        for i, s in enumerate(rules.sections):
            for name in {_norm(s.title), *(_norm(a) for a in s.aliases)}:
                self._by_title.setdefault(name, []).append(i)
        self.seen: set[int] = set()

    def resolve(self, heading: str) -> tuple[Section | None, bool]:
        """Return ``(section, duplicate)``; ``section`` is ``None`` for an unknown heading."""
        number, title = _split_number(heading)
        candidates = self._by_title.get(title)
        if not candidates:
            return None, False
        if number is not None:
            numbered = [
                i
                for i in candidates
                if self._rules.sections[i].number
                and _norm(self._rules.sections[i].number) == number
            ]
            if numbered:
                candidates = numbered
        for i in candidates:
            if i not in self.seen:
                self.seen.add(i)
                return self._rules.sections[i], False
        return self._rules.sections[candidates[0]], True


def check_sections(
    rules: RuleSet, headings: list[str], *, region: Literal["ich", "eu"] = "ich"
) -> list[Finding]:
    """Report duplicate sections, sections out of order and missing required sections.

    Headings that match no section, or a section of another region, are ignored.
    """
    allowed = _allowed(region)
    index = {s.id: i for i, s in enumerate(rules.sections)}
    resolver = _Resolver(rules)
    findings: list[Finding] = []
    present: set[str] = set()
    last: Section | None = None
    for heading in headings:
        section, duplicate = resolver.resolve(heading)
        if section is None or section.region not in allowed:
            continue
        if duplicate:
            findings.append(
                Finding(section.id, section.clause, f"duplicate section {section.ref}: {heading!r}")
            )
            continue
        present.add(section.id)
        if last is not None and index[section.id] < index[last.id]:
            findings.append(
                Finding(
                    section.id,
                    section.clause,
                    f"wrong order: section {section.ref} appears after {last.ref}",
                )
            )
        else:
            last = section
    for section in rules.sections_for(region):
        if section.required and section.id not in present:
            findings.append(
                Finding(section.id, section.clause, f"missing required section {section.ref}")
            )
    return findings


def check_mah_markers(
    rules: RuleSet, sections: dict[str, str], *, supplied: Collection[str] = ()
) -> list[Finding]:
    """Every ``mah_data`` section in ``sections`` (heading -> body) must contain
    ``MAH data required``, unless its section id is in ``supplied`` (the caller gave
    company data). v1 never supplies data."""
    resolver = _Resolver(rules)
    findings: list[Finding] = []
    for heading, body in sections.items():
        section, _ = resolver.resolve(heading)
        if section is None or not section.mah_data or section.id in supplied:
            continue
        if MAH_MARKER not in (body or ""):
            findings.append(
                Finding(
                    section.id,
                    section.clause,
                    f"section {section.ref} needs company data and must say {MAH_MARKER!r}",
                )
            )
    return findings


def headings(rules: RuleSet, region: str = "ich") -> list[str]:
    """Canonical headings of every section of ``region``, in rule order."""
    return [s.heading for s in rules.sections_for(region)]
