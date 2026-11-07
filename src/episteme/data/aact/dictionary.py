"""The AACT data dictionary: tables, columns, types and flags.

``aact_dictionary.csv`` is a byte copy of the owner's download
``aact_data_dictionary_2026-11-03.csv`` (52 tables, 472 columns). Its columns are
``Section, Category, Table, Column, Type, Nullable, PK, FK, References, Unique, Deprecated,
Description, Example``. The file name of the copy has no date, so ``DICTIONARY_DATE``
holds it; the loader records it with each release.

The dictionary does not drive the table columns (the file header does). The loader uses it
to report differences and to find the ``date`` columns of ``studies``.
"""

from __future__ import annotations

import csv
from pathlib import Path

DICTIONARY_PATH = Path(__file__).with_name("aact_dictionary.csv")
DICTIONARY_DATE = "2026-11-03"


def read_dictionary(path: Path | None = None) -> dict[str, list[dict]]:
    """``{table: [row, ...]}`` in file order; each row is a dict keyed by the CSV header.
    ``path`` defaults to the packaged copy. A byte-order mark is ignored."""
    out: dict[str, list[dict]] = {}
    with Path(path or DICTIONARY_PATH).open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            table = (row.get("Table") or "").strip()
            if table:
                out.setdefault(table, []).append(row)
    return out


def columns(dictionary: dict[str, list[dict]], table: str) -> list[str]:
    """The column names of ``table`` in the dictionary (empty for an unknown table)."""
    return [(r.get("Column") or "").strip() for r in dictionary.get(table, [])]


def date_columns(dictionary: dict[str, list[dict]], table: str) -> list[str]:
    """The columns of ``table`` that the dictionary types as ``date``."""
    return [
        (r.get("Column") or "").strip()
        for r in dictionary.get(table, [])
        if (r.get("Type") or "").strip().lower() == "date"
    ]


def deprecated_columns(dictionary: dict[str, list[dict]], table: str) -> list[str]:
    """The columns of ``table`` that the dictionary flags as deprecated."""
    return [
        (r.get("Column") or "").strip()
        for r in dictionary.get(table, [])
        if (r.get("Deprecated") or "").strip().lower() == "yes"
    ]
