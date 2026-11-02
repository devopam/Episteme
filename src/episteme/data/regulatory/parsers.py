"""Parsers for the regulatory feed formats: ``parse(feed, path) -> Iterator[dict]``.

Every parser streams, so memory stays bounded whatever the file size. A new format
is one function added to ``PARSERS``.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import ijson
import openpyxl

from episteme.data.regulatory.feeds import Feed

# An Article 57 title block holds one to three cells per row; the column header row
# is the first row with at least this many non-empty cells. The header row moves
# between releases (row 7 in the design, row 20 in the file fetched on 2026-11-02).
_MIN_HEADER_CELLS = 3


def ema_total(path: Path) -> int | None:
    """``meta.total_records`` of an EMA JSON feed, or ``None`` when absent.
    Reads only as far as the ``meta`` object, which comes first in the feeds."""
    with open(path, "rb") as fh:
        for meta in ijson.items(fh, "meta"):
            total = meta.get("total_records") if isinstance(meta, dict) else None
            return int(total) if total is not None else None
    return None


def _ema_json(feed: Feed, path: Path) -> Iterator[dict]:
    """Rows of ``data`` in an EMA JSON feed. A file cut short raises ``ijson``'s
    incomplete-JSON error. The caller compares the row count with ``ema_total``."""
    with open(path, "rb") as fh:
        yield from ijson.items(fh, "data.item", use_float=True)


def _column_name(cell: object) -> str:
    """First line of a header cell as snake case: "Product name\n\nNote" gives product_name."""
    first = str(cell).strip().splitlines()[0] if str(cell).strip() else ""
    return re.sub(r"[^a-z0-9]+", "_", first.lower()).strip("_")


def _article57_xlsx(feed: Feed, path: Path) -> Iterator[dict]:
    """Rows of the Article 57 workbook, keyed by the header row's column names.
    The workbook is read in read-only mode, one row at a time."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        names: list[str] | None = None
        for row in ws.iter_rows(values_only=True):
            filled = [c for c in row if c is not None and str(c).strip() != ""]
            if names is None:
                if len(filled) >= _MIN_HEADER_CELLS:
                    names = []
                    for i, cell in enumerate(row):
                        col = _column_name(cell) if cell is not None else ""
                        col = col or f"column_{i + 1}"
                        while col in names:
                            col += "_2"
                        names.append(col)
                continue
            if not filled:
                continue
            yield {
                name: (str(v).strip() or None) if isinstance(v, str) else v
                for name, v in zip(names, row, strict=False)
            }
        if names is None:
            raise ValueError(f"no header row found in {path.name}")
    finally:
        wb.close()


def _openfda_json(feed: Feed, path: Path) -> Iterator[dict]:
    """Records of ``results`` in every JSON member of an openFDA zip, streamed."""
    with zipfile.ZipFile(path) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".json"):
                continue
            with zf.open(member) as fh:
                yield from ijson.items(fh, "results.item", use_float=True)


PARSERS: dict[str, Callable[[Feed, Path], Iterator[dict]]] = {
    "ema_json": _ema_json,
    "article57_xlsx": _article57_xlsx,
    "openfda_json": _openfda_json,
}


def parse(feed: Feed, path: Path) -> Iterator[dict]:
    """Stream the records of ``path`` with the parser named by ``feed.format``."""
    try:
        fn = PARSERS[feed.format]
    except KeyError:
        raise ValueError(f"no parser for format {feed.format!r} (feed {feed.name})") from None
    return fn(feed, Path(path))
