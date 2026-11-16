"""Library of Congress ISO 639-2 UTF-8 list. The code is the bibliographic alpha-3."""

from __future__ import annotations

from episteme.data.refmeta.model import CodeRelease, CodeRow


def parse_iso639(text: str, *, notice: str, source_file: str, release_id: str) -> CodeRelease:
    rows: list[CodeRow] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 5:
            raise ValueError(f"{source_file}:{line_no}: expected 5 pipe-separated fields")
        bibliographic, terminologic, alpha2, english, french = (p.strip() for p in parts)
        if not bibliographic:
            raise ValueError(f"{source_file}:{line_no}: bibliographic code is empty")
        rows.append(
            CodeRow(
                code=bibliographic,
                display=english or None,
                extra={
                    "alpha3_terminologic": terminologic,
                    "alpha2": alpha2,
                    "french": french,
                },
            )
        )
    return CodeRelease(
        system_key="iso639",
        release_id=release_id,
        licence_notice=notice,
        source_file=source_file,
        codes=tuple(rows),
    )
