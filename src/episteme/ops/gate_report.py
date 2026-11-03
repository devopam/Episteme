"""The data-quality measures of a sample run and the gate report files
(operating standard spec 7).

``measure_staging`` reads the staging ``*.parquet`` and ``*.jsonl`` shards with
DuckDB. Every measure is an SQL aggregate, so memory stays small: the text is
never loaded into Python. ``measure_load`` reads the sample database after a
sample load. ``write_report`` writes ``gate_report.json`` and ``gate_report.md``.

The measure keys that ``gate.evaluate`` reads are fixed (see ``gate``):
``rows``, ``rows_per_input``, ``text_bytes_per_input_byte``, ``duplicate_ids``,
``duplicate_ids_in_shard``, ``empty_text_pct``, ``extract_status_pct``,
``null_pct``, ``license_counts``, ``failed_inputs``, ``orphan_bodies``,
``rows_loaded_equals_staging``. ``distinct_ids`` is the row count a sample load
must reach. A measure that cannot be computed is None (or absent): its rule then fails.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import duckdb

from episteme.data import article_schema
from episteme.ops.profile import PART_ROW_OVERHEAD_BYTES

_TEXT_COLUMNS = ("title", "abstract", "body_text", "text")
_BODY_COLUMNS = ("text", "body_text", "abstract")
# The shard file of each row (DuckDB's ``filename`` option): the loader loads one
# shard per transaction, so a duplicate id inside one shard breaks the load.
_SHARD_COLUMN = "_gate_shard"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pct(part: int | float, whole: int | float) -> float:
    # Exact: a rule compares this value, so rounding could hide a rare row
    # (1 in 25,000 is 0.004 %). The Markdown report rounds for display.
    return 100.0 * part / whole if whole else 0.0


def _size(path: Path) -> int:
    try:
        if path.is_dir():
            return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        return path.stat().st_size
    except OSError:
        return 0


def _shards(staging_dir: Path) -> tuple[list[str], list[str]]:
    parquet = sorted(p.as_posix() for p in staging_dir.glob("*.parquet"))
    jsonl = sorted(p.as_posix() for p in staging_dir.glob("*.jsonl"))
    return parquet, jsonl


def _lit(paths: list[str]) -> str:
    return "[" + ", ".join("'" + p.replace("'", "''") + "'" for p in paths) + "]"


def _open_view(con: Any, parquet: list[str], jsonl: list[str]) -> list[str]:
    """Create view ``t`` over all shards; return its column names."""
    selects = []
    shard = f"filename = '{_SHARD_COLUMN}'"
    if parquet:
        selects.append(
            f"SELECT * FROM read_parquet({_lit(parquet)}, union_by_name = true, {shard})"
        )
    if jsonl:
        selects.append(
            f"SELECT * FROM read_json_auto({_lit(jsonl)}, union_by_name = true, "
            f"format = 'newline_delimited', {shard})"
        )
    con.execute("CREATE VIEW t AS " + " UNION ALL BY NAME ".join(selects))
    return [r[0] for r in con.execute("DESCRIBE t").fetchall()]


def _blank(col: str) -> str:
    return f"({_q(col)} IS NULL OR trim(CAST({_q(col)} AS VARCHAR)) IN ('', '[]'))"


def _measure_shards(con: Any, columns: list[str], rows: int) -> dict:
    out: dict[str, Any] = {}

    null_pct: dict[str, float] = {}
    present = [c for c in article_schema.ARTICLE_COLUMNS if c in columns]
    if present and rows:
        exprs = ", ".join(f"count(*) FILTER (WHERE {_blank(c)})" for c in present)
        counts = con.execute(f"SELECT {exprs} FROM t").fetchone()
        null_pct = {c: _pct(n, rows) for c, n in zip(present, counts, strict=True)}
    for c in article_schema.ARTICLE_COLUMNS:
        if c not in null_pct:
            null_pct[c] = 100.0
    out["null_pct"] = null_pct

    body_present = [c for c in _BODY_COLUMNS if c in columns]
    if not rows:
        out["empty_text_pct"] = 0.0
    elif body_present:
        cond = " AND ".join(_blank(c) for c in body_present)
        n = con.execute(f"SELECT count(*) FILTER (WHERE {cond}) FROM t").fetchone()[0]
        out["empty_text_pct"] = _pct(n, rows)
    else:
        out["empty_text_pct"] = 100.0

    lengths: dict[str, dict] = {}
    for c in _TEXT_COLUMNS:
        if c not in columns or not rows:
            continue
        p50, p99, mx = con.execute(
            f"SELECT quantile_disc(l, 0.5), quantile_disc(l, 0.99), max(l) FROM "
            f"(SELECT strlen(CAST({_q(c)} AS VARCHAR)) AS l FROM t "
            f"WHERE {_q(c)} IS NOT NULL)"
        ).fetchone()
        lengths[c] = {"p50": p50, "p99": p99, "max": mx}
    out["text_length"] = lengths

    text_bytes = 0
    if "text" in columns and rows:
        text_bytes = con.execute(
            f"SELECT coalesce(sum(strlen(CAST({_q('text')} AS VARCHAR))), 0) FROM t"
        ).fetchone()[0]
    out["text_bytes"] = int(text_bytes)

    for key, col in (("duplicate_ids", "id"), ("duplicate_content_hash", "content_hash")):
        if col in columns and rows:
            out[key] = int(
                con.execute(
                    f"SELECT count({_q(col)}) - count(DISTINCT {_q(col)}) FROM t"
                ).fetchone()[0]
            )
        else:
            # No such column: nothing to duplicate when empty; unknown otherwise.
            out[key] = 0 if not rows else None

    # The loader keeps one row per id across shards (its id-collision guard
    # deletes an older row of the same id under another source_file), so a
    # sample load holds ``distinct_ids`` rows. Inside one shard both rows are
    # copied in one transaction and the article_body key breaks the load.
    if "id" in columns and rows:
        distinct = con.execute(f"SELECT count(DISTINCT {_q('id')}) FROM t").fetchone()[0]
        in_shard = con.execute(
            f"SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM t "
            f"WHERE {_q('id')} IS NOT NULL GROUP BY {_q(_SHARD_COLUMN)}, {_q('id')})"
        ).fetchone()[0]
        out["distinct_ids"] = int(distinct)
        out["duplicate_ids_in_shard"] = int(in_shard)
    else:
        out["distinct_ids"] = 0 if not rows else None
        out["duplicate_ids_in_shard"] = 0 if not rows else None

    def _group(col: str) -> dict[str, int] | None:
        if not rows:
            return {}
        if col not in columns:
            return None  # cannot be computed: the rule that reads it fails
        res = con.execute(
            f"SELECT coalesce(CAST({_q(col)} AS VARCHAR), 'null'), count(*) FROM t GROUP BY 1"
        ).fetchall()
        return {k: int(v) for k, v in sorted(res)}

    status = _group("extract_status")
    out["extract_status"] = status
    out["extract_status_pct"] = (
        None if status is None else {k: _pct(v, rows) for k, v in status.items()}
    )
    out["license_counts"] = _group("license")
    out["subset_counts"] = _group("subset")
    return out


def _measure_parts(con: Any, columns: list[str], rows: int) -> dict:
    """Per ``source_file`` (one part of an input): the distinct names and the
    largest part by rows and by estimated bytes (``strlen`` of ``text`` plus
    ``PART_ROW_OVERHEAD_BYTES`` per row; a NULL text counts 0 bytes).

    The byte maximum covers only parts with 2 or more rows: a single row
    larger than ``part_bytes`` goes alone in its own part (the serializer
    cannot split a row), and that part is the one case allowed over the byte
    cap. When every part has one row, the byte maximum is 0."""
    out: dict[str, Any] = {
        "source_files": [],
        "max_rows_per_source_file": None,
        "max_est_bytes_per_source_file": None,
    }
    if not rows or "source_file" not in columns:
        return out
    text = f"coalesce(strlen(CAST({_q('text')} AS VARCHAR)), 0)" if "text" in columns else "0"
    res = con.execute(
        f"SELECT {_q('source_file')}, count(*), sum({text}) FROM t "
        f"WHERE {_q('source_file')} IS NOT NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    if res:
        out["source_files"] = [str(r[0]) for r in res]
        out["max_rows_per_source_file"] = int(max(r[1] for r in res))
        out["max_est_bytes_per_source_file"] = int(
            max(
                (int(r[2]) + int(r[1]) * PART_ROW_OVERHEAD_BYTES for r in res if int(r[1]) > 1),
                default=0,
            )
        )
    return out


def measure_staging(
    staging_dir: Path,
    *,
    inputs: list[Path],
    record: dict | None = None,
) -> dict:
    """The measures of the shards under ``staging_dir``. An empty folder gives
    ``rows = 0``, not an error. ``record`` is the run record of ``gate run``
    (``elapsed_s``, ``rc``, ``peak_rss_mb``, optional ``failed_inputs``).
    ``text_length`` p50/p99/max are byte lengths (``strlen``), not characters.
    A folder with both .parquet and .jsonl shards raises ``ValueError``."""
    staging_dir = Path(staging_dir)
    parquet, jsonl = _shards(staging_dir)
    if parquet and jsonl:
        raise ValueError(
            f"{staging_dir}: mixes .parquet and .jsonl shards; the gate reads one format per folder"
        )
    input_bytes = sum(_size(Path(p)) for p in inputs)
    measures: dict[str, Any] = {
        "staging_dir": str(staging_dir),
        "shards": len(parquet) + len(jsonl),
        "inputs": len(inputs),
        "input_bytes": input_bytes,
    }

    con = duckdb.connect(":memory:")
    try:
        columns: list[str] = []
        rows = 0
        if parquet or jsonl:
            columns = _open_view(con, parquet, jsonl)
            rows = int(con.execute("SELECT count(*) FROM t").fetchone()[0])
        measures["rows"] = rows
        measures.update(_measure_shards(con, columns, rows))
        measures.update(_measure_parts(con, columns, rows))
    finally:
        con.close()

    measures["rows_per_input"] = rows / len(inputs) if inputs else None
    measures["text_bytes_per_input_byte"] = (
        measures["text_bytes"] / input_bytes if input_bytes else None
    )

    if record is not None:
        elapsed = record.get("elapsed_s")
        measures["elapsed_s"] = elapsed
        measures["peak_rss_mb"] = record.get("peak_rss_mb")
        measures["rows_per_s"] = rows / elapsed if elapsed else None
        if record.get("failed_inputs") is not None:
            measures["failed_inputs"] = record["failed_inputs"]
        elif record.get("rc") == 0:
            measures["failed_inputs"] = 0
        else:
            measures["failed_inputs"] = None
    return measures


def measure_load(
    conn: Any,
    source: str,
    source_files: list[str],
    *,
    staging_rows: int | None = None,
) -> dict:
    """The measures of the sample database after a sample load.

    ``rows_loaded``: ``episteme.articles`` rows of ``source`` with a ``source_file``
    in ``source_files``. ``orphan_bodies``: ``episteme.article_body`` rows of
    ``source`` with no ``episteme.articles`` row of the same id and source. It
    covers the whole source (``article_body`` has no ``source_file``), so leftover
    rows of earlier sample runs in the database count too.
    ``rows_loaded_equals_staging`` is None when ``staging_rows`` is not given."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source = %s AND source_file = ANY(%s)",
            (source, list(source_files)),
        )
        loaded = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM episteme.article_body b WHERE b.source = %s "
            "AND NOT EXISTS (SELECT 1 FROM episteme.articles a "
            "WHERE a.id = b.article_id AND a.source = b.source)",
            (source,),
        )
        orphans = int(cur.fetchone()[0])
    return {
        "rows_loaded": loaded,
        "orphan_bodies": orphans,
        "rows_loaded_equals_staging": None if staging_rows is None else loaded == staging_rows,
    }


# ---------------------------------------------------------------------------
# Sources with no ``articles`` staging: ema and openfda (``reg_record``) and
# openfda_faers (typed tables). Each has a measure function in SOURCE_MEASURES,
# called as ``fn(conn, raw_dir=, run_id=, record=)``. The article measures above
# stay as they are.
# ---------------------------------------------------------------------------
_NAME_CHUNK = 5000


def coverage_pct(names: Iterable[str], known: set[str]) -> float | None:
    """The share (percent) of the distinct ``names`` that are in ``known``; None when there
    is no name, because a share of nothing is not a pass."""
    distinct = set(names)
    if not distinct:
        return None
    return _pct(len(distinct & known), len(distinct))


def meta_total_equals_rows(checks: dict[str, tuple[int, int | None]]) -> bool | None:
    """Whether every feed in ``checks`` (name -> (rows, declared total)) holds as many rows
    as it declares. A feed with no declared total is a failure; no feed is None."""
    if not checks:
        return None
    return all(total is not None and rows == total for rows, total in checks.values())


def record_measures(record: dict) -> dict:
    """The measures of the run record: time, peak memory, failed inputs (0 on exit code 0,
    the record's own count when it has one, else None)."""
    if record.get("failed_inputs") is not None:
        failed = record["failed_inputs"]
    elif record.get("rc") == 0:
        failed = 0
    else:
        failed = None
    return {
        "elapsed_s": record.get("elapsed_s"),
        "peak_rss_mb": record.get("peak_rss_mb"),
        "failed_inputs": failed,
    }


def _rows_of_run(cur: Any, run_id: str | None) -> int | None:
    """The sum of ``rows_affected`` over this run's load audit rows. It is an event sum, not a
    row count: the recall_lot rows and the per-zip and per-generation rows are in it too. It
    only tells whether the run did work: 0 for a run that loaded nothing (a marker or
    fingerprint said the data was current), so ``min_rows`` fails and a pass never certifies
    data from an earlier run. Never raise ``min_rows`` above 1 from it. None without a run id."""
    if not run_id:
        return None
    cur.execute(
        "SELECT coalesce(sum(rows_affected), 0) FROM episteme._audit "
        "WHERE run_id = %s AND event_type IN ('load_commit', 'load_replace')",
        (run_id,),
    )
    return int(cur.fetchone()[0])


def _known_names(cur: Any, names: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(names), _NAME_CHUNK):
        cur.execute(
            "SELECT id_value FROM episteme.substance_xref "
            "WHERE id_type = 'name' AND id_value = ANY(%s)",
            (names[i : i + _NAME_CHUNK],),
        )
        found.update(r[0] for r in cur.fetchall())
    return found


def measure_reg(conn: Any, source: str, *, raw_dir: Path, run_id: str | None, record: dict) -> dict:
    """Measures of ``ema`` or ``openfda`` after a sample load: for each feed that has files
    in the slice, the rows of its latest release (``rows_by_dataset``), whether the rows
    equal the file's own ``meta.total_records`` (``meta_total_equals_rows``, EMA JSON feeds)
    and the share of its distinct normalised substance names that have a ``name`` row in
    ``substance_xref`` (``xref_coverage_pct``, feeds with substance fields). ``rows`` is
    what this run loaded. The release is read first and passed as a value."""
    from episteme.data.regulatory.load_reg import (
        EXPECTED_TOTAL,
        feed_files,
        input_keys,
        latest_release,
        select_feeds,
    )
    from episteme.data.regulatory.normalize import norm_name

    rows_by_dataset: dict[str, int] = {}
    checks: dict[str, tuple[int, int | None]] = {}
    coverage: dict[str, float | None] = {}
    with conn.cursor() as cur:
        for feed in select_feeds(source, None):
            paths = feed_files(source, feed, raw_dir)
            if not paths:
                continue
            release = latest_release(conn, feed.name)
            keys = input_keys(paths, raw_dir)
            rows = 0
            if release is not None:
                cur.execute(
                    "SELECT count(*) FROM episteme.reg_record "
                    "WHERE dataset = %s AND release_id = %s AND source_file = ANY(%s)",
                    (feed.name, release, keys),
                )
                rows = int(cur.fetchone()[0])
            rows_by_dataset[feed.name] = rows
            reader = EXPECTED_TOTAL.get(feed.format)
            if reader is not None and len(paths) == 1:
                checks[feed.name] = (rows, reader(paths[0]))
            if feed.substance_fields and release is not None:
                cur.execute(
                    "SELECT DISTINCT n FROM episteme.reg_record r, unnest(r.substance_names) n "
                    "WHERE r.dataset = %s AND r.release_id = %s AND r.source_file = ANY(%s)",
                    (feed.name, release, keys),
                )
                names = sorted({norm_name(r[0]) for r in cur.fetchall() if r[0]} - {""})
                coverage[feed.name] = coverage_pct(names, _known_names(cur, names))
        out: dict[str, Any] = {
            "rows": _rows_of_run(cur, run_id),
            "rows_by_dataset": rows_by_dataset,
            "meta_total_equals_rows": meta_total_equals_rows(checks),
            "xref_coverage_pct": coverage,
        }
    out.update(record_measures(record))
    return out


def temp_bytes_run(record: dict) -> int | None:
    """The temp bytes the database wrote during the run: the change of
    ``pg_stat_database.temp_bytes`` between the snapshots that ``gate run`` took before and
    after the stage. None when a snapshot is missing."""
    before, after = record.get("db_temp_bytes_before"), record.get("db_temp_bytes_after")
    if before is None or after is None:
        return None
    return max(0, int(after) - int(before))


def measure_faers(conn: Any, *, raw_dir: Path, run_id: str | None, record: dict) -> dict:
    """Measures of ``openfda_faers`` after a sample load: the live generation's rows, the
    duplicate report ids (the de-duplication leaves none), the years it spans and the
    largest year, the signal rows, the share of drug entries that the bridge links to a
    substance (``xref_coverage_pct["faers_drug_entry"]``), and
    ``db_temp_bytes_run``, the temp bytes the database wrote during the run (the change of
    ``pg_stat_database.temp_bytes`` between snapshots of ``gate run``; includes the signal
    build). ``peak_rss_mb`` is the client process tree only: the signal build is SQL on the
    server, whose memory is bounded by ``work_mem`` and is not measured here. ``counts``
    holds the counts that ``min_counts`` reads, so a build that made nothing fails."""
    out: dict[str, Any] = {}
    with conn.cursor() as cur:
        out["rows"] = _rows_of_run(cur, run_id)
        cur.execute(
            "SELECT count(*), count(*) - count(DISTINCT safetyreportid), "
            "min(receive_year), max(receive_year) FROM episteme.faers_report"
        )
        reports, dup, y_min, y_max = cur.fetchone()
        out.update(
            faers_reports=int(reports),
            duplicate_ids=int(dup),
            receive_year_min=y_min,
            receive_year_max=y_max,
        )
        cur.execute("SELECT count(*) FROM episteme.faers_drug")
        out["faers_drug_rows"] = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM episteme.faers_reaction")
        out["faers_reaction_rows"] = int(cur.fetchone()[0])
        cur.execute(
            "SELECT receive_year, count(*) FROM episteme.faers_report "
            "GROUP BY 1 ORDER BY 2 DESC, 1 DESC LIMIT 1"
        )
        top = cur.fetchone()
        out["largest_year"] = top[0] if top else None
        out["largest_year_reports"] = int(top[1]) if top else 0
        cur.execute("SELECT count(*) FROM episteme.faers_pair_quarter")
        out["signal_pair_rows"] = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM episteme.faers_event_quarter")
        out["signal_event_rows"] = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM episteme.faers_entry_substance s "
            "WHERE s.drug_entry_id = e.drug_entry_id)), count(*) FROM episteme.faers_drug_entry e"
        )
        linked, entries = cur.fetchone()
        out["xref_coverage_pct"] = {
            "faers_drug_entry": _pct(int(linked), int(entries)) if entries else None
        }
    out["db_temp_bytes_run"] = temp_bytes_run(record)
    out["counts"] = {
        "faers_reports": out["faers_reports"],
        "signal_pair_rows": out["signal_pair_rows"],
        "signal_event_rows": out["signal_event_rows"],
        "largest_year_reports": out["largest_year_reports"],
    }
    out.update(record_measures(record))
    return out


SOURCE_MEASURES: dict[str, Callable[..., dict]] = {
    "ema": partial(measure_reg, source="ema"),
    "openfda": partial(measure_reg, source="openfda"),
    "openfda_faers": measure_faers,
}


def _display(value: Any) -> Any:
    """Round floats to 6 significant digits for the Markdown report only."""
    if isinstance(value, float):
        return float(f"{value:.6g}")
    if isinstance(value, dict):
        return {k: _display(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_display(v) for v in value]
    return value


def _fmt(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _render_md(measures: dict, failures: list[str]) -> str:
    lines = ["# Sample gate report", ""]
    lines.append(f"Result: {'FAIL' if failures else 'PASS'}")
    lines += ["", "## Failed rules", ""]
    lines += [f"- {f}" for f in failures] or ["- (none)"]
    lines += ["", "## Measures", "", "| measure | value |", "| --- | --- |"]
    for key in sorted(measures):
        value = _display(measures[key])
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True)
        lines.append(f"| {key} | {str(_fmt(value)).replace('|', chr(92) + '|')} |")
    return "\n".join(lines) + "\n"


def write_report(measures: dict, failures: list[str], out_dir: Path) -> Path:
    """Write ``gate_report.json`` and ``gate_report.md`` in ``out_dir``; return
    the path of the JSON file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": not failures,
        "failures": list(failures),
        "measures": measures,
    }
    json_path = out_dir / "gate_report.json"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    (out_dir / "gate_report.md").write_text(_render_md(measures, failures), encoding="utf-8")
    return json_path
