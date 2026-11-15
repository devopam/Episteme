"""Regenstrief LOINC table. Group 1 fields are copied under their published names."""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from episteme.data.refmeta.model import CodeRelease, CodeRow

_REQUIRED = ("LOINC_NUM", "LONG_COMMON_NAME", "STATUS")
_EXTRA = (
    "COMPONENT",
    "PROPERTY",
    "TIME_ASPCT",
    "SYSTEM",
    "SCALE_TYP",
    "METHOD_TYP",
    "CLASS",
)


def parse_loinc_zip(path: Path, *, notice: str, source_file: str, release_id: str) -> CodeRelease:
    with zipfile.ZipFile(path) as archive:
        name = next(
            (
                info.filename
                for info in archive.infolist()
                if info.filename.endswith("Loinc.csv") and not info.is_dir()
            ),
            None,
        )
        if name is None:
            raise ValueError(f"{path}: no Loinc.csv member")
        text = archive.read(name).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    missing = [col for col in _REQUIRED if col not in (reader.fieldnames or ())]
    if missing:
        raise ValueError(f"{path}: Loinc.csv is missing {', '.join(missing)}")
    rows: list[CodeRow] = []
    for rec in reader:
        code = (rec.get("LOINC_NUM") or "").strip()
        if not code:
            raise ValueError(f"{path}: a row has an empty LOINC_NUM")
        rows.append(
            CodeRow(
                code=code,
                display=(rec.get("LONG_COMMON_NAME") or "").strip() or None,
                status=(rec.get("STATUS") or "").strip() or None,
                extra={col: (rec.get(col) or "").strip() for col in _EXTRA if rec.get(col)},
            )
        )
    return CodeRelease(
        system_key="loinc",
        release_id=release_id,
        version=release_id,
        licence_notice=notice,
        source_file=source_file,
        codes=tuple(rows),
    )
