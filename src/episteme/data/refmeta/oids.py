"""OID catalogue from ICH E2B(R3) IG v5.03 section 3.2, tables 1 to 8.

Appendix I (F) of this edition is not the catalogue. It says the code lists are
published as XML beside the guide. The OIDs are the tables in section 3.2.
"""

from __future__ import annotations

import re
from pathlib import Path

from pypdf import PdfReader

from episteme.data.refmeta.model import CodeRelease, CodeRow

ICH_NOTICE = (
    "ICH E2B(R3) IG v5.03 (18 July 2025), section 3.2 tables 1-8. "
    "Use, reproduce and adapt with ICH credit; mark changes; do not imply "
    "ICH endorsement. Third-party term content is not stored. "
    "Appendix I (F) of this edition points at the XML code lists and holds no OID table."
)

_OID = re.compile(r"(?<![\d.])((?:1\.0|2\.16)(?:\.\d+)+)")
_ELEMENT = re.compile(
    r"(?:ACK(?:\.[A-Za-z]+)+(?:\.r)?(?:\.\d+)?|[CDEFGHN]\.(?:[0-9a-z]+\.)*[0-9a-z]+)"
)
_TABLE = re.compile(r"Table\s+([1-8])\s*:")
_ISO_IDMP = re.compile(r"ISO\s*(?:11615|11616|11238|11239)")


def parse_oid_text(text: str, *, source_file: str, release_id: str = "ig-v5.03") -> CodeRelease:
    """Turn the extracted text of the OID pages into one ``e2b_oid`` release."""
    rows: list[CodeRow] = []
    matches = list(_TABLE.finditer(text))
    if not matches:
        raise ValueError(f"{source_file}: no OID tables (Table 1 to Table 8)")
    for index, match in enumerate(matches):
        number = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = " ".join(text[match.end() : end].split())
        rows.extend(_table_rows(number, body))
    return CodeRelease(
        system_key="e2b_oid",
        release_id=release_id,
        version="5.03",
        licence_notice=ICH_NOTICE,
        source_file=source_file,
        codes=tuple(rows),
    )


def parse_oid_pdf(path: Path, *, source_file: str, release_id: str = "ig-v5.03") -> CodeRelease:
    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if "Table " in text and ("OID" in text or "ISO11615" in text or "ISO 11615" in text):
            parts.append(text)
    if not parts:
        raise ValueError(f"{path}: no OID table pages")
    return parse_oid_text("\n".join(parts), source_file=source_file, release_id=release_id)


def _table_rows(number: str, body: str) -> list[CodeRow]:
    if number == "1":
        return _idmp_rows(body)
    rows: list[CodeRow] = []
    cursor = 0
    for found in _OID.finditer(body):
        chunk = body[cursor : found.start()]
        oid = found.group(1)
        element, name = _last_element(chunk)
        code = f"t{number}:{element or name}"
        rows.append(
            CodeRow(
                code=code[:240],
                display=name or None,
                extra={"table": number, "element_id": element, "oid": oid},
            )
        )
        cursor = found.end()
    if not rows:
        raise ValueError(f"OID table {number} has no OID")
    return rows


def _idmp_rows(body: str) -> list[CodeRow]:
    rows: list[CodeRow] = []
    cursor = 0
    for found in _ISO_IDMP.finditer(body):
        chunk = body[cursor : found.start()]
        element, name = _last_element(chunk)
        if not element:
            cursor = found.end()
            continue
        ref = re.sub(r"\s+", "", found.group(0))
        tail = body[found.end() : found.end() + 48]
        tail = tail.split(" D.")[0].split(" G.")[0].strip()
        scheme = f"{ref} {tail}".strip()
        rows.append(
            CodeRow(
                code=f"t1:{element}"[:240],
                display=name or None,
                status="pending_oid",
                extra={"table": "1", "element_id": element, "oid": None, "scheme": scheme},
            )
        )
        cursor = found.end()
    if len(rows) != 10:
        got = ", ".join(row.extra["element_id"] for row in rows)
        raise ValueError(f"IDMP OID table has {len(rows)} rows ({got}); expected 10")
    return rows


def _last_element(chunk: str) -> tuple[str, str]:
    found = list(_ELEMENT.finditer(chunk))
    if not found:
        return "", re.sub(r"\s+", " ", chunk).strip(" -–:")
    last = found[-1]
    name = re.sub(r"\s+", " ", chunk[last.end() :]).strip(" -–:")
    return last.group(0), name
