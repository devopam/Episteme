"""Parquet / JSONL writers for episteme.articles rows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from episteme.data import article_schema
from episteme.data.article_schema import ARTICLE_COLUMNS


def rows_to_columnar(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    cols: dict[str, list[Any]] = {c: [] for c in ARTICLE_COLUMNS}
    for r in rows:
        for c in ARTICLE_COLUMNS:
            cols[c].append(r.get(c))
    return cols


def write_parquet_shard(
    rows: list[dict[str, Any]],
    staging_root: Path,
    *,
    source: str,
    source_file: str,
) -> list[Path]:
    """
    Write one Parquet file for all rows under:
      staging_root/staging/<source>/<stem>.parquet
    """
    if not rows:
        return []

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError(
            "pyarrow is required for Parquet output. Install with: pip install pyarrow"
        ) from e

    base = Path(staging_root) / "staging" / source
    base.mkdir(parents=True, exist_ok=True)

    stem = Path(source_file).name
    # sanitize filename
    stem = stem.replace("/", "_")
    if stem.endswith(".gz"):
        stem = stem[:-3]
    if stem.endswith(".xml"):
        stem = stem[:-4]

    out_path = base / f"{stem}.parquet"

    col_data = rows_to_columnar(rows)
    # list columns as list<string>
    arrays = {}
    for c, values in col_data.items():
        if c in ("authors", "mesh", "publication_types"):
            arrays[c] = pa.array(values, type=pa.list_(pa.string()))
        elif c == "year":
            arrays[c] = pa.array(
                [None if v is None else int(v) for v in values],
                type=pa.int32(),
            )
        elif c in ("is_retracted", "is_manuscript", "is_historical_ocr"):
            arrays[c] = pa.array(values, type=pa.bool_())
        else:
            # NOTE: any jsonb-typed DB column (currently only book_meta) MUST be
            # pre-serialized to a JSON string (json.dumps(...)) by the caller before
            # it reaches this function -- a raw dict lands here and becomes Python's
            # str(dict) repr (single-quoted, INVALID JSON), not real JSON. See
            # extract_bookshelf.py's book_meta handling for the reference pattern.
            arrays[c] = pa.array(
                [None if v is None else str(v) if not isinstance(v, str) else v for v in values],
                type=pa.string(),
            )
    table = pa.table(arrays)
    pq.write_table(table, out_path, compression="zstd")

    return [out_path]


def write_jsonl_shard(
    rows: list[dict[str, Any]],
    staging_root: Path,
    *,
    source: str,
    source_file: str,
) -> Path:
    """Fallback writer when pyarrow is unavailable."""
    base = Path(staging_root) / "staging" / source
    base.mkdir(parents=True, exist_ok=True)
    stem = Path(source_file).name
    # sanitize filename
    stem = stem.replace("/", "_")
    if stem.endswith(".gz"):
        stem = stem[:-3]
    if stem.endswith(".xml"):
        stem = stem[:-4]
    out_path = base / f"{stem}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return out_path


def write_rows(
    rows: list[dict[str, Any]],
    staging_root: Path,
    *,
    source: str,
    source_file: str,
    prefer_parquet: bool = True,
) -> dict[str, Any]:
    assert source in article_schema.SOURCES, f"unknown source {source!r}"
    if prefer_parquet:
        try:
            paths = write_parquet_shard(rows, staging_root, source=source, source_file=source_file)
            return {"format": "parquet", "paths": [str(p) for p in paths], "n_rows": len(rows)}
        except RuntimeError:
            pass
    path = write_jsonl_shard(rows, staging_root, source=source, source_file=source_file)
    return {"format": "jsonl", "paths": [str(path)], "n_rows": len(rows)}
