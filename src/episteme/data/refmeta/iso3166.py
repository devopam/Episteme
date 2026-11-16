"""Owner-placed ISO 3166-1 code elements. The code is the alpha-2 code."""

from __future__ import annotations

import csv
import io

from episteme.data.refmeta.model import CodeRelease, CodeRow

HEADER = ("alpha_2", "alpha_3", "numeric", "short_name_en")


def parse_iso3166(text: str, *, notice: str, source_file: str, release_id: str) -> CodeRelease:
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != HEADER:
        raise ValueError(
            f"{source_file}: header must be {','.join(HEADER)}; got {reader.fieldnames}"
        )
    rows: list[CodeRow] = []
    for rec in reader:
        alpha2 = (rec["alpha_2"] or "").strip()
        if not alpha2:
            raise ValueError(f"{source_file}: a row has an empty alpha_2")
        rows.append(
            CodeRow(
                code=alpha2,
                display=(rec["short_name_en"] or "").strip() or None,
                extra={
                    "alpha_3": (rec["alpha_3"] or "").strip(),
                    "numeric": (rec["numeric"] or "").strip(),
                },
            )
        )
    return CodeRelease(
        system_key="iso3166",
        release_id=release_id,
        licence_notice=notice,
        source_file=source_file,
        codes=tuple(rows),
    )
