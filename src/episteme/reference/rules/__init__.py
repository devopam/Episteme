"""Versioned rule store: implementation guides and guidelines held as packaged JSON.

Each rule file (``data/<ig_id>.json``) names its IG, version and source documents with
their sha256, and lists checkable items. Every item keeps the clause of its source.

Section checks (``check_sections``, ``check_mah_markers``) match draft headings to rule
sections: case-insensitive, punctuation-insensitive, leading section number optional.
Both checks use one resolver, so a caller can pass ``list(draft)`` and ``draft``.

Rule data is read-only: mappings are ``MappingProxyType`` and lists are tuples.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from importlib import resources
from types import MappingProxyType
from typing import Any, Literal

MAH_MARKER = "MAH data required"

_IG_ID = re.compile(r"^[a-z0-9_]+$")
_REGIONS: dict[str, frozenset[str]] = {
    "ich": frozenset({"ich"}),
    "eu": frozenset({"ich", "eu"}),
}
_DOC_BY_REGION = {"ich": "ICH E2C(R2)", "eu": "EU GVP"}
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
    source_docs: tuple[Mapping[str, str], ...]
    sections: tuple[Section, ...] = ()
    data: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), repr=False, compare=False
    )

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
    return parse_rules(ig_id, json.loads(path.read_text(encoding="utf-8")))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def parse_rules(ig_id: str, data: Mapping[str, Any]) -> RuleSet:
    """Build a read-only ``RuleSet`` from rule-file data. Raises ``ValueError`` that
    names the section when a section has an unknown region."""
    data = _freeze(data)
    for s in data.get("sections", ()):
        if s.get("region") not in _REGIONS:
            raise ValueError(
                f"{ig_id}: section {s.get('id')!r} has region {s.get('region')!r};"
                f" expected one of {sorted(_REGIONS)}"
            )
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
        source_docs=data["source_docs"],
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
    """Map headings to sections in one pass, in draft order.

    - A heading with a number resolves only to a section with that number, or to an
      unnumbered section of that title (the EU regional-appendix sub-sections). If
      neither exists, the heading is unresolved.
    - A title shared by two sections (6.1 and Appendix 1 "Reference Information";
      16.5 and A6.5 "Effectiveness of risk minimisation") resolves without a number by
      position: to the first unmatched candidate after the last resolved section,
      else to the first unmatched candidate.
    """

    def __init__(self, rules: RuleSet) -> None:
        self._rules = rules
        self._by_title: dict[str, list[int]] = {}
        for i, s in enumerate(rules.sections):
            for name in {_norm(s.title), *(_norm(a) for a in s.aliases)}:
                self._by_title.setdefault(name, []).append(i)
        self.seen: set[int] = set()
        self._last = -1

    def resolve(self, heading: str) -> tuple[Section | None, bool]:
        """Return ``(section, duplicate)``; ``section`` is ``None`` for an unknown heading."""
        number, title = _split_number(heading)
        candidates = self._by_title.get(title)
        if not candidates:
            return None, False
        if number is not None:
            sections = self._rules.sections
            numbered = [
                i for i in candidates if sections[i].number and _norm(sections[i].number) == number
            ]
            candidates = numbered or [i for i in candidates if not sections[i].number]
            if not candidates:
                return None, False
        unseen = [i for i in candidates if i not in self.seen]
        if not unseen:
            return self._rules.sections[candidates[0]], True
        i = next((i for i in unseen if i > self._last), unseen[0])
        self.seen.add(i)
        self._last = i
        return self._rules.sections[i], False


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
    placed: list[Section] = []
    for heading in headings:
        section, duplicate = resolver.resolve(heading)
        if section is None or section.region not in allowed:
            continue
        if duplicate:
            findings.append(
                Finding(section.id, section.clause, f"duplicate section {section.ref}: {heading!r}")
            )
            continue
        placed.append(section)
    keep = _longest_increasing([index[s.id] for s in placed])
    for pos, section in enumerate(placed):
        if pos in keep:
            continue
        where = []
        if pos > 0:
            where.append(f"after {placed[pos - 1].ref}")
        if pos + 1 < len(placed):
            where.append(f"before {placed[pos + 1].ref}")
        findings.append(
            Finding(
                section.id,
                section.clause,
                f"wrong order: section {section.ref} is placed {' and '.join(where)}",
            )
        )
    present = {s.id for s in placed}
    for section in rules.sections_for(region):
        if section.required and section.id not in present:
            findings.append(
                Finding(section.id, section.clause, f"missing required section {section.ref}")
            )
    return findings


def _longest_increasing(values: list[int]) -> set[int]:
    """Positions of one longest strictly increasing subsequence. On a tie the earlier
    element is kept, so for a swapped pair the later heading is the one flagged."""
    if not values:
        return set()
    length = [1] * len(values)
    prev = [-1] * len(values)
    for i in range(len(values)):
        for j in range(i):
            if values[j] < values[i] and length[j] + 1 > length[i]:
                length[i], prev[i] = length[j] + 1, j
    pos = max(range(len(values)), key=lambda i: (length[i], -i))
    keep: set[int] = set()
    while pos != -1:
        keep.add(pos)
        pos = prev[pos]
    return keep


def check_mah_markers(
    rules: RuleSet,
    sections: dict[str, str],
    *,
    supplied: Collection[str] = (),
    strict: bool = False,
) -> list[Finding]:
    """Every ``mah_data`` section in ``sections`` (heading -> body) must contain
    ``MAH data required``, unless its section id is in ``supplied`` (the caller gave
    company data). v1 never supplies data.

    With ``strict``, the body must be the marker alone (after strip): a marker plus any
    other content is a finding, because no company data is supplied."""
    resolver = _Resolver(rules)
    findings: list[Finding] = []
    for heading, body in sections.items():
        section, _ = resolver.resolve(heading)
        if section is None or not section.mah_data or section.id in supplied:
            continue
        if strict and MAH_MARKER in (body or "") and (body or "").strip() != MAH_MARKER:
            findings.append(
                Finding(
                    section.id,
                    section.clause,
                    f"section {section.ref} holds {MAH_MARKER!r} plus other content;"
                    " no company data is supplied",
                )
            )
        elif MAH_MARKER not in (body or ""):
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
