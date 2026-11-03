"""SmPC sections of an EPAR ``product-information`` PDF, as offsets into the stored text.

The PDF holds Annex I (the SmPC, one or more products), Annex II, the labelling and the
package leaflet. Sections are read from Annex I only: the leaflet has its own numbered
headings ("4. Possible side effects") that must not be taken. The match is deterministic
(line-anchored regular expressions on the EMA SmPC template); there is no model and no
fuzzy text match.

Layout (EMA SmPC template)::

    ANNEX I
    SUMMARY OF PRODUCT CHARACTERISTICS
    1. NAME OF THE MEDICINAL PRODUCT
    <product name>
    ...
    4. CLINICAL PARTICULARS
    4.1 Therapeutic indications
    ...
    7. MARKETING AUTHORISATION HOLDER
    ANNEX II
    ...

Every pattern is a constant of this module, in one place, so a check against real text
(the sample gate) changes this file only. Change the rule: raise ``SMPC_RULE_VERSION``;
the EPAR fingerprint of ``product-information`` documents carries it, so those documents
are extracted again.

``smpc_sections(body_text)`` returns one dict per section found, in document order:
``loinc_code`` (the DailyMed code where the spec table has one, else ``smpc:<number>``),
``title`` (the heading as printed, plus `` - <product>`` when the PDF holds several
SmPCs), ``ordinal`` (1-based position in the document), ``product`` (the line after
"1. NAME OF THE MEDICINAL PRODUCT", or ``None``), ``start_offset`` and ``end_offset``
(characters into ``body_text``; ``body_text[start:end]`` is the heading and its text).
A document without a match gives ``[]``.
"""

from __future__ import annotations

import re
from typing import Any

# Version of the rule below. Part of the fingerprint of product-information documents.
SMPC_RULE_VERSION = 1

# Spec 2026-11-03 section 5: SmPC number -> the DailyMed LOINC code of the same subject.
SMPC_LOINC: dict[str, str] = {
    "4.1": "34067-9",
    "4.2": "34068-7",
    "4.3": "34070-3",
    "4.4": "43685-7",
    "4.5": "34073-7",
    "4.6": "42228-7",
    "4.8": "34084-4",
    "4.9": "34088-5",
    "5.1": "43679-0",
    "5.2": "43682-4",
}

# --- the heading pattern (one place) ---------------------------------------------------
# A stripped line, compared whole.
SMPC_START_RE = re.compile(r"^SUMMARY OF PRODUCT CHARACTERISTICS$")
ANNEX_ONE_RE = re.compile(r"^ANNEX I$")
ANNEX_OTHER_RE = re.compile(r"^ANNEX (?:II|III|IV|V|VI)$")  # ends Annex I
NAME_HEADING_RE = re.compile(r"^1\.?\s+NAME OF THE MEDICINAL PRODUCT\b", re.IGNORECASE)
# 4.1 to 4.9, 5.1 to 5.3, 6.1 to 6.6; the title starts with a capital letter.
SECTION_HEADING_RE = re.compile(r"^(4\.[1-9]|5\.[1-3]|6\.[1-6])\s+([A-Z].{2,150})$")
# A top-level SmPC heading ("5. PHARMACOLOGICAL PROPERTIES", "7. MARKETING AUTHORISATION
# HOLDER"): it ends the section before it and starts no row.
TOP_HEADING_RE = re.compile(r"^\d{1,2}\.\s+[A-Z][A-Z0-9 ,;:'()/&-]{3,}$")
_MAX_PRODUCT_CHARS = 200


def loinc_for(number: str) -> str:
    """The ``label_section.loinc_code`` of an SmPC number: mapped, else ``smpc:<number>``."""
    return SMPC_LOINC.get(number, f"smpc:{number}")


def _num_key(number: str) -> tuple[int, int]:
    major, minor = number.split(".")
    return int(major), int(minor)


def _lines(text: str):
    """``(start offset, stripped line)`` for every line of ``text``."""
    pos = 0
    for raw in text.split("\n"):
        lead = len(raw) - len(raw.lstrip())
        yield pos + lead, raw.strip()
        pos += len(raw) + 1


def _split_smpcs(text: str) -> list[dict[str, Any]]:
    """The SmPCs of Annex I: each has ``name`` and ``events``, a list of
    ``("h", number, title, offset)`` headings and ``("stop", offset)`` marks, plus
    ``end`` (the offset where its region ends)."""
    smpcs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    awaiting_name = False
    prev: tuple[int, str] | None = None  # the last non-blank line
    last_num: tuple[int, int] = (0, 0)

    def close(at: int) -> None:
        nonlocal cur
        if cur is not None:
            cur["end"] = at
            smpcs.append(cur)
            cur = None

    for pos, line in _lines(text):
        if not line:
            continue
        if SMPC_START_RE.match(line):
            close(prev[0] if prev is not None and ANNEX_ONE_RE.match(prev[1]) else pos)
            cur = {"name": None, "events": [], "end": len(text)}
            awaiting_name = False
            last_num = (0, 0)
        elif ANNEX_OTHER_RE.match(line):
            close(pos)
            awaiting_name = False
        elif cur is not None:
            if awaiting_name:
                if not (SECTION_HEADING_RE.match(line) or TOP_HEADING_RE.match(line)):
                    cur["name"] = line[:_MAX_PRODUCT_CHARS]
                awaiting_name = False
            elif NAME_HEADING_RE.match(line):
                awaiting_name = True
                cur["events"].append(("stop", pos))
            else:
                m = SECTION_HEADING_RE.match(line)
                if m and _num_key(m.group(1)) > last_num:
                    last_num = _num_key(m.group(1))
                    cur["events"].append(("h", m.group(1), line, pos))
                elif TOP_HEADING_RE.match(line):
                    cur["events"].append(("stop", pos))
        prev = (pos, line)
    close(len(text))
    return smpcs


def smpc_sections(body_text: str) -> list[dict[str, Any]]:
    """The SmPC sections of ``body_text`` (see the module doc); ``[]`` when none is found."""
    if not body_text:
        return []
    smpcs = [s for s in _split_smpcs(body_text) if any(e[0] == "h" for e in s["events"])]
    several = len(smpcs) > 1
    rows: list[dict[str, Any]] = []
    for smpc in smpcs:
        events = smpc["events"]
        for i, ev in enumerate(events):
            if ev[0] != "h":
                continue
            _, number, title, start = ev
            end = events[i + 1][-1] if i + 1 < len(events) else smpc["end"]
            end = start + len(body_text[start:end].rstrip())
            product = smpc["name"]
            rows.append(
                {
                    "loinc_code": loinc_for(number),
                    "title": f"{title} - {product}" if several and product else title,
                    "ordinal": len(rows) + 1,
                    "product": product,
                    "start_offset": start,
                    "end_offset": end,
                }
            )
    return rows
