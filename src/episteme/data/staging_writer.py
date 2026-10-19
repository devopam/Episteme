"""Parquet / JSONL writers for episteme.articles rows."""

from __future__ import annotations

import itertools
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from episteme.data import article_schema
from episteme.data.article_schema import ARTICLE_COLUMNS
from episteme.ops.profile import default_batch_rows

WRITE_BATCH_ROWS = default_batch_rows()


def rows_to_columnar(rows: list[dict[str, Any]]) -> dict[str, list[Any]]:
    cols: dict[str, list[Any]] = {c: [] for c in ARTICLE_COLUMNS}
    for r in rows:
        for c in ARTICLE_COLUMNS:
            cols[c].append(r.get(c))
    return cols


def _pyarrow_available() -> bool:
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        return False
    return True


def _out_path(staging_root: Path, source: str, source_file: str, suffix: str) -> Path:
    base = Path(staging_root) / "staging" / source
    base.mkdir(parents=True, exist_ok=True)
    stem = Path(source_file).name
    # sanitize filename
    stem = stem.replace("/", "_")
    if stem.endswith(".gz"):
        stem = stem[:-3]
    if stem.endswith(".xml"):
        stem = stem[:-4]
    return base / f"{stem}{suffix}"


def _article_schema():
    import pyarrow as pa

    fields = []
    for c in ARTICLE_COLUMNS:
        if c in ("authors", "mesh", "publication_types"):
            fields.append(pa.field(c, pa.list_(pa.string())))
        elif c == "year":
            fields.append(pa.field(c, pa.int32()))
        elif c in ("is_retracted", "is_manuscript", "is_historical_ocr"):
            fields.append(pa.field(c, pa.bool_()))
        else:
            fields.append(pa.field(c, pa.string()))
    return pa.schema(fields)


def _batch_to_table(rows: list[dict[str, Any]]):
    """Type one batch of rows with the fixed article schema (identical for every batch)."""
    import pyarrow as pa

    schema = _article_schema()
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
    return pa.table(arrays, schema=schema)


def _write_parquet_stream(
    rows: Iterable[dict[str, Any]],
    staging_root: Path,
    *,
    source: str,
    source_file: str,
    batch_size: int,
) -> tuple[list[Path], int]:
    """Stream rows into one Parquet file (one row group per batch); returns (paths, n_rows)."""
    import pyarrow.parquet as pq

    it = iter(rows)
    first = list(itertools.islice(it, batch_size))
    if not first:
        return [], 0

    out_path = _out_path(staging_root, source, source_file, ".parquet")
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    n = 0
    try:
        with pq.ParquetWriter(tmp_path, _article_schema(), compression="zstd") as writer:
            batch = first
            while batch:
                writer.write_table(_batch_to_table(batch))
                n += len(batch)
                batch = list(itertools.islice(it, batch_size))
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return [out_path], n


def _write_jsonl_stream(
    rows: Iterable[dict[str, Any]],
    staging_root: Path,
    *,
    source: str,
    source_file: str,
) -> tuple[Path, int]:
    out_path = _out_path(staging_root, source, source_file, ".jsonl")
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    n = 0
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
                n += 1
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return out_path, n


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

    if not _pyarrow_available():
        raise RuntimeError(
            "pyarrow is required for Parquet output. Install with: pip install pyarrow"
        )

    paths, _ = _write_parquet_stream(
        rows, staging_root, source=source, source_file=source_file, batch_size=len(rows)
    )
    return paths


def write_jsonl_shard(
    rows: list[dict[str, Any]],
    staging_root: Path,
    *,
    source: str,
    source_file: str,
) -> Path:
    """Fallback writer when pyarrow is unavailable."""
    path, _ = _write_jsonl_stream(rows, staging_root, source=source, source_file=source_file)
    return path


def write_rows(
    rows: Iterable[dict[str, Any]],
    staging_root: Path,
    *,
    source: str,
    source_file: str,
    prefer_parquet: bool = True,
    batch_size: int = WRITE_BATCH_ROWS,
) -> dict[str, Any]:
    assert source in article_schema.SOURCES, f"unknown source {source!r}"
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if prefer_parquet and _pyarrow_available():
        paths, n = _write_parquet_stream(
            rows, staging_root, source=source, source_file=source_file, batch_size=batch_size
        )
        return {"format": "parquet", "paths": [str(p) for p in paths], "n_rows": n}
    path, n = _write_jsonl_stream(rows, staging_root, source=source, source_file=source_file)
    return {"format": "jsonl", "paths": [str(path)], "n_rows": n}
