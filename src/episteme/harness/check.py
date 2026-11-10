"""Deterministic post-answer check and claim router. No model call."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import date
from typing import Literal

from episteme.config import DEFAULT_HARNESS_MAX_CANDIDATES
from episteme.reference.claims import Claim, LookupResult

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_MONTH_NO = {m: i for i, m in enumerate(MONTHS.split("|"), 1)}
# Order matters: an identifier match hides its digits from the later patterns.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("id", re.compile(r"EMEA/H/C/PSUSA/\d+/\d+")),
    ("id", re.compile(r"\bNCT\d{8}\b")),
    ("iso", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("long", re.compile(rf"\b(\d{{1,2}}) ({MONTHS}) (\d{{4}})\b")),
    ("ndc", re.compile(r"\b\d{4,5}-\d{3,4}(?:-\d{1,2})?\b")),
    ("pmid", re.compile(r"\bPMID:?\s*(\d+)\b", re.IGNORECASE)),
    ("id", re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10}\b")),
    ("int", re.compile(r"(?<![\w.\-/,])(?:\d{1,3}(?:,\d{3})+|\d{2,})(?![\w\-/]|[.,]\d)")),
)
# Numbers in claim text: any digit run; a thousands group also gives its parts.
_LOOSE_INT = re.compile(r"(?<![\w.\-/:])\d+(?:,\d{3})*(?![\w\-/:]|\.\d)")
_Key = tuple[str, object]

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


def _is_year_range(text: str) -> bool:
    """``2015-2020`` fits the two-segment NDC shape; it is a range of years."""
    left, _, right = text.partition("-")
    return len(left) == len(right) == 4 and left[:2] in ("19", "20") and right[:2] in ("19", "20")


def _key(kind: str, m: re.Match[str]) -> _Key:
    """Canonical form: dates compare by value, every other token as written."""
    if kind == "iso":
        try:
            return ("date", date.fromisoformat(m.group(0)))
        except ValueError:
            return ("raw", m.group(0))
    if kind == "long":
        try:
            return ("date", date(int(m.group(3)), _MONTH_NO[m.group(2)], int(m.group(1))))
        except ValueError:
            return ("raw", m.group(0))
    if kind == "pmid":
        return ("pmid", m.group(1))
    if kind == "int":
        return ("int", m.group(0).replace(",", ""))
    return ("raw", m.group(0))


def _scan(text: str) -> list[tuple[str, _Key, str, int, int]]:
    """Tokens of ``text`` as (kind, key, written form, start, end); no token overlaps."""
    taken = [False] * len(text)
    found = []
    for kind, pat in _PATTERNS:
        for m in pat.finditer(text):
            if any(taken[m.start() : m.end()]):
                continue
            if kind == "ndc" and _is_year_range(m.group(0)):
                continue
            for i in range(m.start(), m.end()):
                taken[i] = True
            written = m.group(1) if kind == "pmid" else m.group(0)
            found.append((kind, _key(kind, m), written, m.start(), m.end()))
    return found


def _stated(hay: str) -> set[_Key]:
    keys: set[_Key] = set()
    masked = list(hay)
    for kind, key, _, start, end in _scan(hay):
        if kind != "int":
            keys.add(key)
            masked[start:end] = " " * (end - start)
    keys.update(("pmid", d) for d in re.findall(r"(?<!\d)\d+(?!\d)", hay))
    for m in _LOOSE_INT.finditer("".join(masked)):
        keys.add(("int", m.group(0).replace(",", "")))
        if "," in m.group(0):
            keys.update(("int", part) for part in m.group(0).split(","))
    return keys


def check_answer(answer: str, results: list[LookupResult]) -> list[str]:
    """Return the dates, ids and counts in ``answer`` that no claim states.

    Dates compare by value, so ``25 January 2020`` matches ``2020-01-25``. Every other
    token must match a whole token of a claim value, text or provenance key."""
    stated = _stated(_haystack(results))
    offending: list[str] = []
    for _, key, written, _, _ in sorted(_scan(answer), key=lambda t: t[3]):
        if key not in stated and written not in offending:
            offending.append(written)
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


def _terms_in(claim: str, terms: tuple[str, ...]) -> bool:
    # Both ends need a word edge; a plural s or a digit may follow (``NCT01234567``).
    return any(re.search(rf"\b{re.escape(t)}s?(?![A-Za-z])", claim, re.IGNORECASE) for t in terms)


def route_claim(
    claim: str,
    resolve: Callable[[str], LookupResult],
    max_candidates: int = DEFAULT_HARNESS_MAX_CANDIDATES,
) -> tuple[Literal["lookup", "model"], str | None]:
    """Send a claim to a lookup when it names a resolvable entity and a trigger term."""
    function = next((f for f, terms in FIELD_TERMS.items() if _terms_in(claim, terms)), None)
    if function is None:
        return ("model", None)
    trigger_stems = {
        w.lower().rstrip("s") for terms in FIELD_TERMS.values() for t in terms for w in t.split()
    }
    candidates = [m.group(0) for m in _ID_RE.finditer(claim)]
    candidates += [
        w
        for w in dict.fromkeys(_WORD_RE.findall(claim))
        if w.lower() not in _STOP and w.lower().rstrip("s") not in trigger_stems
    ]
    for cand in candidates[:max_candidates]:
        if resolve(cand).claims:
            return ("lookup", function)
    return ("model", None)
