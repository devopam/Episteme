"""Deterministic post-answer check and claim router. No model call."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from typing import Literal

from episteme.reference.claims import Claim, LookupResult

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
# Order matters: an identifier match hides its digits from the later patterns.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("epr", re.compile(r"EMEA/H/C/PSUSA/\d+/\d+")),
    ("nct", re.compile(r"\bNCT\d{8}\b")),
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("date", re.compile(rf"\b\d{{1,2}} (?:{MONTHS}) \d{{4}}\b")),
    ("ndc", re.compile(r"\b\d{4,5}-\d{3,4}-\d{1,2}\b")),
    ("pmid", re.compile(r"\bPMID:?\s*(\d+)\b", re.IGNORECASE)),
    ("unii", re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10}\b")),
    ("int", re.compile(r"(?<![\w.\-/,])(?:\d{1,3}(?:,\d{3})+|\d{2,})(?![\w\-/]|[.,]\d)")),
)

FIELD_TERMS: dict[str, tuple[str, ...]] = {
    "approval_status": ("approved", "authorised", "authorized", "marketing authorisation"),
    "safety_actions": ("recall", "referral", "DHPC", "PSUSA"),
    "current_label": ("label", "contraindication", "boxed warning", "section"),
    "trials": ("trial", "NCT", "phase"),
    "faers_counts": ("adverse event report", "FAERS", "reports"),
}


def _claims(results: list[LookupResult]) -> Iterator[Claim]:
    stack = [c for r in results for c in r.claims]
    while stack:
        c = stack.pop()
        yield c
        stack.extend(c.alternatives)


def _haystack(results: list[LookupResult]) -> str:
    parts: list[str] = []
    for c in _claims(results):
        parts.extend((c.value or "", c.text, c.provenance.record_key or ""))
    return "\n".join(parts)


def check_answer(answer: str, results: list[LookupResult]) -> list[str]:
    """Return the dates, ids and counts in ``answer`` that no claim states."""
    hay = _haystack(results)
    hay_plain = hay.replace(",", "")
    taken = [False] * len(answer)
    offending: list[str] = []
    for kind, pat in _PATTERNS:
        for m in pat.finditer(answer):
            if any(taken[m.start() : m.end()]):
                continue
            for i in range(m.start(), m.end()):
                taken[i] = True
            if kind == "pmid":
                value, found = m.group(1), re.search(rf"(?<!\d){m.group(1)}(?!\d)", hay)
            elif kind == "int":
                value = m.group(0)
                plain = value.replace(",", "")
                found = re.search(rf"(?<![\d.]){plain}(?!\d)", hay_plain)
            else:
                value, found = m.group(0), m.group(0) in hay
            if not found and value not in offending:
                offending.append(value)
    return offending


_ID_RE = re.compile(
    r"\b(?:NCT\d{8}|(?:NDA|ANDA|BLA)\s?\d{4,6}|\d{4,5}-\d{3,4}-\d{1,2}"
    r"|(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10})\b"
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z-]{3,}")
_STOP = frozenset(
    "this that with from what which does there their about have been into than then"
    " when where whose will would should could them they were your also only over".split()
)
_MAX_CANDIDATES = 8


def _terms_in(claim: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(t)}", claim, re.IGNORECASE) for t in terms)


def route_claim(
    claim: str, resolve: Callable[[str], LookupResult]
) -> tuple[Literal["lookup", "model"], str | None]:
    """Send a claim to a lookup when it names a resolvable entity and a trigger term."""
    function = next((f for f, terms in FIELD_TERMS.items() if _terms_in(claim, terms)), None)
    if function is None:
        return ("model", None)
    trigger_words = {w.lower() for terms in FIELD_TERMS.values() for t in terms for w in t.split()}
    candidates = [m.group(0) for m in _ID_RE.finditer(claim)]
    candidates += [
        w
        for w in dict.fromkeys(_WORD_RE.findall(claim))
        if w.lower() not in _STOP and w.lower() not in trigger_words
    ]
    for cand in candidates[:_MAX_CANDIDATES]:
        if resolve(cand).claims:
            return ("lookup", function)
    return ("model", None)
