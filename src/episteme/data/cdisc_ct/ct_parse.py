"""Pure parser and row builder for CDISC Controlled Terminology (CT), as
published quarterly by NCI Enterprise Vocabulary Services (EVS).

Real file shape (verified 2026-09-28 against the current SDTM/SEND/ADaM/
Define-XML/Protocol releases -- see docs/superpowers/specs/
2026-09-28-sp7-cdisc-controlled-terminology-design.md secs 3, 4.2): tab-
separated, UTF-8, 8 columns -- ``Code``, ``Codelist Code``, ``Codelist
Extensible (Yes/No)``, ``Codelist Name``, ``CDISC Submission Value``,
``CDISC Synonym(s)``, ``CDISC Definition``, ``NCI Preferred Term``. A
**codelist header row** carries an empty ``Codelist Code`` (its own ``Code``
is the codelist's NCI concept code); a **term row** carries its codelist's
code in ``Codelist Code``. Nothing in the file guarantees a codelist's
header row appears before its term rows -- this module reads the whole file
first and assembles codelists afterwards, so a term row that happens to
precede its own header still attaches correctly.

This module is intentionally free of I/O beyond reading the one input file:
no database, no network, no other episteme modules besides
``article_schema`` for the row schema/licence helpers. The download and
load stages (SP7 later tasks) own everything else.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from episteme.data.article_schema import (
    LICENSE_PUBLIC_DOMAIN,
    finalize_row,
    normalize_license,
    subset_from_license,
)

SOURCE = "cdisc_ct"

# Exact 8-column header, in order, as it appears on line 1 of every real
# CDISC CT release file (SDTM/SEND/ADaM/Define-XML/Protocol all share this
# shape -- design spec sec 3).
EXPECTED_HEADER: tuple[str, ...] = (
    "Code",
    "Codelist Code",
    "Codelist Extensible (Yes/No)",
    "Codelist Name",
    "CDISC Submission Value",
    "CDISC Synonym(s)",
    "CDISC Definition",
    "NCI Preferred Term",
)

# NCI's licence statement for CDISC Controlled Terminology, exact text from
# the plan's Global Constraints (docs/superpowers/plans/
# 2026-09-28-sp7-cdisc-controlled-terminology.md).
_CT_LICENSE_RAW = "NCI EVS: CDISC Terminology is free to use without licensing restrictions."


class CTFormatError(ValueError):
    """Raised when a CDISC CT file does not match the expected tab-separated,
    8-column shape: a header row that isn't exactly ``EXPECTED_HEADER``, or a
    later non-blank row that doesn't have exactly 8 cells."""


@dataclass
class Term:
    code: str
    submission_value: str
    synonyms: str
    definition: str
    preferred_term: str


@dataclass
class Codelist:
    code: str
    name: str
    submission_value: str
    extensible: str
    definition: str
    preferred_term: str
    terms: list[Term] = field(default_factory=list)


def parse_ct_file(path: Path) -> tuple[list[Codelist], dict[str, int]]:
    """Parse one CDISC CT release file into its codelists (in the order
    their header rows appeared) plus counters for rows that could not be
    attached: ``skipped_no_id`` (empty ``Code``) and ``orphan_terms`` (a term
    row whose ``Codelist Code`` never has a matching header row anywhere in
    the file)."""
    path = Path(path)
    counts = {"skipped_no_id": 0, "orphan_terms": 0}

    codelist_order: list[str] = []
    codelists: dict[str, Codelist] = {}
    pending_terms: dict[str, list[Term]] = {}

    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        try:
            header = next(reader)
        except StopIteration:
            raise CTFormatError("empty file: missing header row") from None

        header = [cell.strip().lstrip("﻿") for cell in header]
        if tuple(header) != EXPECTED_HEADER:
            raise CTFormatError(f"unexpected header: {header!r}")

        for n, row in enumerate(reader, start=2):
            if not row or (len(row) == 1 and not row[0].strip()):
                continue  # blank line (e.g. trailing newline)
            if len(row) != 8:
                raise CTFormatError(f"line {n}: {len(row)} columns")

            (
                code,
                codelist_code,
                extensible,
                name,
                submission_value,
                synonyms,
                definition,
                preferred_term,
            ) = row
            code = code.strip()
            codelist_code = codelist_code.strip()

            if not code:
                counts["skipped_no_id"] += 1
                continue

            if not codelist_code:
                # codelist header row
                if code in codelists:
                    raise CTFormatError(
                        f"duplicate codelist header row for code {code!r} (line {n})"
                    )
                codelists[code] = Codelist(
                    code=code,
                    name=name,
                    submission_value=submission_value,
                    extensible=extensible,
                    definition=definition,
                    preferred_term=preferred_term,
                )
                codelist_order.append(code)
            else:
                # term row -- attach after the full file has been read, since
                # its codelist's header row may not have appeared yet
                pending_terms.setdefault(codelist_code, []).append(
                    Term(
                        code=code,
                        submission_value=submission_value,
                        synonyms=synonyms,
                        definition=definition,
                        preferred_term=preferred_term,
                    )
                )

    for codelist_code, terms in pending_terms.items():
        codelist = codelists.get(codelist_code)
        if codelist is None:
            counts["orphan_terms"] += len(terms)
            continue
        codelist.terms.extend(terms)

    codelists_out = [codelists[code] for code in codelist_order]
    return codelists_out, counts


def _chunk_terms(terms: list[Term], max_terms: int) -> list[list[Term]]:
    """Split ``terms`` into chunks of at most ``max_terms``. A codelist with
    no terms still yields one (empty) chunk, so it produces a ``p1`` row."""
    if not terms:
        return [[]]
    return [terms[i : i + max_terms] for i in range(0, len(terms), max_terms)]


def _term_line(term: Term) -> str:
    line = f"- {term.submission_value} (NCI {term.code}): {term.preferred_term}."
    if term.synonyms:
        line += f" Synonyms: {term.synonyms}."
    line += f" Definition: {term.definition}"
    return line


def _part_text(
    codelist: Codelist,
    part_terms: list[Term],
    *,
    package: str,
    release_date: str,
    part_index: int,
    n_parts: int,
) -> str:
    lines = [
        f"CDISC {package} Controlled Terminology (NCI EVS release {release_date}).",
        f"Codelist: {codelist.name} (submission value {codelist.submission_value}; "
        f"NCI code {codelist.code}; extensible: {codelist.extensible}).",
        f"Definition: {codelist.definition}",
        f"NCI preferred term: {codelist.preferred_term}",
        f"Terms (part {part_index} of {n_parts}):" if n_parts > 1 else "Terms:",
    ]
    lines.extend(_term_line(term) for term in part_terms)
    return "\n".join(lines)


def build_rows(
    codelists: list[Codelist],
    *,
    package: str,
    release_date: str,
    source_file: str,
    max_terms: int = 200,
) -> list[dict]:
    """One finalized ``episteme.articles`` row per codelist per package, split
    into parts of at most ``max_terms`` terms (each part repeats the codelist
    header so it stands alone). Row dict follows ``serialize_mesh.mesh_row``'s
    column set and licence-override pattern."""
    lic, lic_url, lic_raw = normalize_license(_CT_LICENSE_RAW)
    # GOVERNANCE OVERRIDE (design spec sec 2, user decision 2026-09-28): CDISC
    # Controlled Terminology is treated as public domain -> commercial-eligible,
    # same pattern as MeSH/PubChem/ClinVar. Source-anchored on purpose:
    # normalize_license() never returns this code for free text on its own.
    # license_raw keeps NCI's actual statement.
    lic = LICENSE_PUBLIC_DOMAIN
    subset = subset_from_license(lic)

    rows: list[dict[str, Any]] = []
    for codelist in codelists:
        chunks = _chunk_terms(codelist.terms, max_terms)
        n_parts = len(chunks)
        for i, part_terms in enumerate(chunks, start=1):
            title = codelist.name
            if n_parts > 1:
                title = f"{codelist.name} (part {i} of {n_parts})"

            row: dict[str, Any] = {
                "id": f"{SOURCE}:{package}:{codelist.code}:p{i}",
                "source": SOURCE,
                "source_file": source_file,
                "source_record_id": f"{package}:{codelist.code}:p{i}",
                "pmid": None,
                "pmcid": None,
                "doi": None,
                "title": title,
                "abstract": None,
                "body_text": None,
                "text": _part_text(
                    codelist,
                    part_terms,
                    package=package,
                    release_date=release_date,
                    part_index=i,
                    n_parts=n_parts,
                ),
                "authors": None,
                "journal": None,
                "year": None,
                "mesh": None,
                "publication_types": None,
                "language": None,
                "license": lic,
                "license_url": lic_url,
                "license_raw": lic_raw,
                "subset": subset,
                "is_retracted": False,
                "pmc_version": None,
                "is_manuscript": None,
                "is_historical_ocr": None,
                "pdf_url": None,
                "container_id": None,
                "book_meta": None,
            }
            rows.append(finalize_row(row))

    return rows
