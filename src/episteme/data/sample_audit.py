"""Field-shape report over a source's staging shards.

``build_report`` reads every ``*.parquet`` then ``*.jsonl`` shard under
``staging_dir`` (Polars), concatenates them, and emits a structured ``.json``
plus a human-readable ``.md`` next to them at
``<staging_dir>/../../_ops/<source>/field_shape_report.{json,md}``.

For every column in :data:`article_schema.ARTICLE_COLUMNS` it records the
non-null percentage, the distinct count, and up to 5 stringified sample
values. It also surfaces the distinct ``license`` strings with counts, the
``extract_status`` histogram, and a list of human-readable "surprise" flags
(e.g. a column expected to be populated that is essentially all-null).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import polars as pl

from episteme.data import article_schema

# Columns we expect a real extractor to populate on (almost) every row.
_EXPECT_POPULATED = ("id", "source", "authors")
# text / title / abstract are checked as a group -- a row needs at least one.
_TEXT_COLUMNS = ("text", "title", "abstract")
_NULL_FLAG_PCT = 95.0
_SAMPLE_MAX = 5
_SAMPLE_TRUNC = 80


def _read_shards(staging_dir: Path) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for path in sorted(staging_dir.glob("*.parquet")):
        frames.append(pl.read_parquet(path))
    for path in sorted(staging_dir.glob("*.jsonl")):
        frames.append(pl.read_ndjson(path))
    if not frames:
        raise FileNotFoundError(f"no *.parquet or *.jsonl shards under {staging_dir}")
    if len(frames) == 1:
        return frames[0]
    return pl.concat(frames, how="diagonal_relaxed")


def _stringify(value: object) -> str:
    text = str(value)
    if len(text) > _SAMPLE_TRUNC:
        text = text[: _SAMPLE_TRUNC - 3] + "..."
    return text


def _column_stats(values: list, n_rows: int) -> dict:
    non_null = [v for v in values if v is not None]
    distinct = {json.dumps(v, sort_keys=True, default=str) for v in non_null}
    return {
        "non_null_pct": round(100.0 * len(non_null) / n_rows, 1) if n_rows else 0.0,
        "distinct": len(distinct),
        "samples": [_stringify(v) for v in non_null[:_SAMPLE_MAX]],
    }


def _flags(columns: dict, extract_status_hist: dict, n_rows: int) -> list[str]:
    flags: list[str] = []

    for col in _EXPECT_POPULATED:
        stats = columns.get(col)
        if stats and (100.0 - stats["non_null_pct"]) > _NULL_FLAG_PCT:
            flags.append(f"{col} is {100.0 - stats['non_null_pct']:.1f}% null")

    present_text = [c for c in _TEXT_COLUMNS if c in columns]
    if present_text and all(
        (100.0 - columns[c]["non_null_pct"]) > _NULL_FLAG_PCT for c in present_text
    ):
        flags.append(f"{'/'.join(present_text)} all >{_NULL_FLAG_PCT:.0f}% null")

    year = columns.get("year")
    if year is not None and year["non_null_pct"] == 0.0:
        flags.append("year is 100.0% null (all rows will land in articles_pmc_y0)")

    if n_rows and "ok" not in extract_status_hist:
        flags.append("extract_status has no 'ok' rows")

    return flags


def _render_markdown(report: dict, source: str) -> str:
    lines: list[str] = [
        f"# Field-shape report - {source}",
        "",
        f"- shards: {report['n_shards']}",
        f"- rows: {report['n_rows']}",
        "",
        "## Columns",
        "",
        "| column | non-null % | distinct | samples |",
        "| --- | ---: | ---: | --- |",
    ]
    for col, stats in report["columns"].items():
        samples = "; ".join(s.replace("|", "\\|") for s in stats["samples"])
        lines.append(f"| {col} | {stats['non_null_pct']} | {stats['distinct']} | {samples} |")

    lines += ["", "## license values", ""]
    if report["license_values"]:
        for lic, count in sorted(report["license_values"].items()):
            lines.append(f"- `{lic}`: {count}")
    else:
        lines.append("- (none)")

    lines += ["", "## extract_status histogram", ""]
    if report["extract_status_histogram"]:
        for status, count in sorted(report["extract_status_histogram"].items()):
            lines.append(f"- `{status}`: {count}")
    else:
        lines.append("- (none)")

    lines += ["", "## Surprises", ""]
    if report["flags"]:
        lines += [f"- {flag}" for flag in report["flags"]]
    else:
        lines.append("- (none)")

    return "\n".join(lines) + "\n"


def build_report(staging_dir: Path | str, source: str) -> Path:
    """Read the staging shards under ``staging_dir`` and write the report.

    Returns the path to the ``.md`` file; the structured ``.json`` sits beside
    it. Raises ``FileNotFoundError`` when no shard is found.
    """
    staging_dir = Path(staging_dir)
    df = _read_shards(staging_dir)
    n_rows = df.height

    columns: dict[str, dict] = {}
    for col in article_schema.ARTICLE_COLUMNS:
        if col in df.columns:
            columns[col] = _column_stats(df[col].to_list(), n_rows)

    license_values: dict[str, int] = {}
    if "license" in df.columns:
        license_values = dict(Counter(str(v) for v in df["license"].to_list() if v is not None))

    extract_status_hist: dict[str, int] = {}
    if "extract_status" in df.columns:
        extract_status_hist = {
            ("null" if v is None else str(v)): c
            for v, c in Counter(df["extract_status"].to_list()).items()
        }

    n_shards = len(list(staging_dir.glob("*.parquet"))) + len(list(staging_dir.glob("*.jsonl")))
    report = {
        "source": source,
        "staging_dir": str(staging_dir),
        "n_shards": n_shards,
        "n_rows": n_rows,
        "columns": columns,
        "license_values": license_values,
        "extract_status_histogram": extract_status_hist,
        "flags": _flags(columns, extract_status_hist, n_rows),
    }

    out_dir = staging_dir.parent.parent / "_ops" / source
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "field_shape_report.json"
    md_path = out_dir / "field_shape_report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_render_markdown(report, source), encoding="utf-8")
    return md_path
