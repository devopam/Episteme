#!/usr/bin/env python3
"""Extraction: guidelines — parse ``epfl-llm/guidelines`` HF dataset files
(the "Clinical Guidelines" / Meditron pretraining corpus) into the unified
``episteme.articles`` row schema (contract v1.4).

Unit of work: one HF dataset file under ``raw_dir`` (parquet or jsonl).
Read with **polars lazy** (``pl.scan_parquet`` / ``pl.scan_ndjson``) per the
roadmap's no-pandas constraint: the schema is inspected via
``LazyFrame.collect_schema()`` (no data read) before any row is materialised,
and only the columns this extractor actually uses are ``.select()``-ed before
the final ``.collect()``.

Real dataset schema (confirmed at field-shape sign-off, 2026-09-11, via the HF
``dataset_info`` API + a ranged HTTP read of the live
``epfl-llm/guidelines/open_guidelines.jsonl`` — see task-9-report.md): ONE
file, ``open_guidelines.jsonl``, with columns ``id``, ``source``, ``title``,
``clean_text``, ``raw_text``, ``url``, ``overview`` (all strings; no
``year``/``language`` column exists — the dataset card states English-only
out of band, not per row). ``source`` is the issuing-body tag (``nice``,
``cdc``, ``who``, ``cco``, ...) — NOT this extractor's own ``source`` field,
which is always the literal ``"guidelines"``; the dataset's ``source`` column
is carried into ``journal`` instead (spec §4.7: "the issuing-body field").
A real quirk: ``title``/``url``/``overview`` are populated for only 9 of 17
source tags — for every other row those columns hold the *literal string*
``"None"`` (Python's ``str(None)``, not a JSON null). ``_clean_str`` treats
that literal (and blank/whitespace) as NULL.

**Skip heuristic (QA-shaped splits):** any file whose column names include
(case-insensitively) ``question``, ``answer``, ``options``, ``choices`` or
``answer_idx`` is skipped in full — logged, counted as an input, contributing
0 rows — never processed as prose and never raising. The real
``epfl-llm/guidelines`` release ships no such split (single-file, prose-only,
confirmed above); the heuristic exists as a defensive guard against a future
QA/SFT config being pointed at this extractor's ``raw_dir`` by mistake, and is
exercised here only by the synthetic ``qa_split.jsonl`` fixture.

**Column-name heuristic for the prose/body columns** (checked in priority
order, case-insensitive, first match wins — tuned to the real schema above
but left general for a parquet re-export or a different HF revision):
  * text    -> ``clean_text``, ``raw_text``, ``text``, ``content``, ``body``,
               ``clinician_guideline``
  * title   -> ``title``
  * journal -> ``source``, ``origin``, ``issuing_body``, ``clinical_area``
  * year    -> ``year``, ``publication_year``, ``pub_year``
  * language -> ``language``, ``lang``, ``locale``
  * id      -> ``id``, ``doc_id``, ``uid`` (source_record_id; row index if none)
A file with no recognisable text column at all is skipped the same way as a
QA-shaped one (defensive — never emit all-null rows).

**Licence/subset ruling (controller, pre-dispatch, 2026-09-11)**: clinical
guidelines are individually copyrighted by dozens of issuing bodies with NO
single licence — confirmed at field-shape sign-off: the HF dataset card's
``license: other`` / ``license_name: common-crawl`` metadata resolves to an
EMPTY ``LICENSE`` file, and the card's prose says "please always check
redistribution licenses before using the content... To the best of our
knowledge, we are following the redistribution licensing of each source" —
i.e. no overall permissive/CC-BY statement exists to upgrade this ruling.
Every row therefore gets ``subset = "other"`` (a literal assignment — NOT
``subset_from_license``, mirroring how ``extract_europepmc_manuscripts.py``
hardcodes ``subset = "text_mining"`` for its own spec-mandated reason),
``license = "unknown"``, ``license_raw = "see epfl-llm/guidelines dataset
card (mixed per-issuing-body licences)"``. ``pmid``/``pmcid``/``doi``/``mesh``
and ``container_id``/``book_meta`` are ``None`` on every row (not
PubMed-indexed, not a book).

Importable core: ``extract_guidelines(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (§4.7):
  python -m episteme.data.guidelines.extract_guidelines \\
    --raw-dir ./01_raw/guidelines \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    --workers 4 \\
    [--force]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import polars as pl

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.audit_trail import record as _audit  # noqa: E402
from episteme.config import get_settings  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    ARTICLE_COLUMNS,
    SCHEMA_VERSION,
    finalize_row,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "guidelines"

# Ruling (see module docstring): no single licence covers this corpus.
_LICENSE_RAW = "see epfl-llm/guidelines dataset card (mixed per-issuing-body licences)"

# QA-shaped split guard -- a matching column name (case-insensitive) means
# "skip this whole file", never "parse it as prose". Exact column-name match,
# not substring (so e.g. "clean_text" is never mistaken for "text"-as-QA).
_QA_SHAPE_COLUMNS = {"question", "answer", "options", "choices", "answer_idx"}

# Column-name heuristics, checked in order, case-insensitive, first match wins.
_TEXT_COLUMN_CANDIDATES = (
    "clean_text",
    "raw_text",
    "text",
    "content",
    "body",
    "clinician_guideline",
)
_TITLE_COLUMN_CANDIDATES = ("title",)
_JOURNAL_COLUMN_CANDIDATES = ("source", "origin", "issuing_body", "clinical_area")
_YEAR_COLUMN_CANDIDATES = ("year", "publication_year", "pub_year")
_LANGUAGE_COLUMN_CANDIDATES = ("language", "lang", "locale")
_ID_COLUMN_CANDIDATES = ("id", "doc_id", "uid")

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). extract() itself stays parallel — only this block serializes.
# Mirrors extract_pubmed.py / extract_apollo.py.
_AUDIT_LOCK = threading.Lock()


def _scan(path: Path) -> pl.LazyFrame:
    if path.suffix == ".parquet":
        return pl.scan_parquet(path)
    return pl.scan_ndjson(path)


def _schema_columns(path: Path) -> list[str]:
    """Schema-only introspection -- no row data is read."""
    return _scan(path).collect_schema().names()


def _is_qa_shaped(columns: list[str]) -> bool:
    lowered = {c.lower() for c in columns}
    return bool(lowered & _QA_SHAPE_COLUMNS)


def _pick(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def _clean_str(v: Any) -> str | None:
    """Normalise a cell value to usable text or ``None``.

    The real dataset serialises an absent field as the literal string
    ``"None"`` for many rows (see module docstring) -- treat that literal,
    plus blank/whitespace-only strings, as NULL rather than as content.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s == "None":
        return None
    return s


def _parse_year(v: Any) -> int | None:
    s = _clean_str(v)
    if not s:
        return None
    m = re.search(r"\d{4}", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def iter_rows_from_file(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one ``article_schema`` row per record in one guidelines file.

    Yields nothing (never raises) for a QA-shaped split, or a file with no
    recognisable text column -- both are "skip the whole file", logged by the
    caller, not an error.
    """
    lf = _scan(path)
    columns = lf.collect_schema().names()
    if _is_qa_shaped(columns):
        return

    text_col = _pick(columns, _TEXT_COLUMN_CANDIDATES)
    if text_col is None:
        return

    title_col = _pick(columns, _TITLE_COLUMN_CANDIDATES)
    journal_col = _pick(columns, _JOURNAL_COLUMN_CANDIDATES)
    year_col = _pick(columns, _YEAR_COLUMN_CANDIDATES)
    lang_col = _pick(columns, _LANGUAGE_COLUMN_CANDIDATES)
    id_col = _pick(columns, _ID_COLUMN_CANDIDATES)

    select_cols = sorted(
        {c for c in (text_col, title_col, journal_col, year_col, lang_col, id_col) if c}
    )
    df = lf.select(select_cols).collect()

    source_file = path.name
    for idx, rec in enumerate(df.iter_rows(named=True)):
        text = _clean_str(rec.get(text_col))
        title = _clean_str(rec.get(title_col)) if title_col else None
        journal = _clean_str(rec.get(journal_col)) if journal_col else None
        year = _parse_year(rec.get(year_col)) if year_col else None
        language = _clean_str(rec.get(lang_col)) if lang_col else None
        rec_id = _clean_str(rec.get(id_col)) if id_col else None
        source_record_id = rec_id or str(idx)

        row: dict[str, Any] = {
            "id": f"{SOURCE}:{rec_id}" if rec_id else f"{SOURCE}:{source_file}:{idx}",
            "source": SOURCE,
            "source_file": source_file,
            "source_record_id": source_record_id,
            "pmid": None,
            "pmcid": None,
            "doi": None,
            "title": title,
            "abstract": None,
            "body_text": text,
            # text = the document body, verbatim -- set explicitly so
            # finalize_row does not rebuild it as title + "\n\n" + body
            # (mirrors extract_apollo.py's record_to_row).
            "text": text,
            "authors": None,
            "journal": journal,
            "year": year,
            "mesh": None,
            "publication_types": None,
            "language": language,
            "license": "unknown",
            "license_url": None,
            "license_raw": _LICENSE_RAW,
            # HARDCODED, not subset_from_license(...) -- see module docstring
            # ruling: no single licence covers this mixed-provenance corpus.
            "subset": "other",
            "is_retracted": False,
            "pmc_version": None,
            "is_manuscript": None,
            "is_historical_ocr": None,
            "pdf_url": None,
            "container_id": None,
            "book_meta": None,
        }
        yield finalize_row(row)


def discover(raw_dir: Path) -> list[Path]:
    """One unit of work per HF dataset file (parquet or jsonl)."""
    raw_dir = Path(raw_dir)
    return sorted(raw_dir.glob("*.parquet")) + sorted(raw_dir.glob("*.jsonl"))


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror extract_pubmed.py: one chained ``extract_commit`` audit row on a
    fresh connection, degrading to the file-only mirror when no DB is reachable.
    Never raises — ``mark_success`` has already run for this file."""
    with _AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "extract_commit",
                    conn=_conn,
                    object=f"{SOURCE} {basename}",
                    rows_affected=n_rows,
                    reason=None,
                    run_id=get_settings().run_id,
                )
                _conn.commit()
        except Exception:  # noqa: BLE001 - best-effort: DB unavailable or audit failed
            try:
                from episteme import audit_trail as _audit_trail

                _audit_trail.mirror_only(
                    "extract_commit",
                    object=f"{SOURCE} {basename}",
                    rows_affected=n_rows,
                    note="db_unavailable_or_failed",
                    run_id=get_settings().run_id,
                )
            except Exception:  # noqa: BLE001 - last-resort fallback must never escape
                _LOG.warning(
                    "audit mirror_only fallback also failed for %s", basename, exc_info=True
                )


def process_one(
    path: Path,
    *,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    basename = path.name
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    try:
        for row in iter_rows_from_file(path):
            status_counts[str(row.get("extract_status"))] += 1
            rows.append(row)

        qa_shaped_skip = not rows and _is_qa_shaped(_schema_columns(path))

        write_info = write_rows(
            rows,
            processed_dir,
            source=SOURCE,
            source_file=basename,
            prefer_parquet=True,
        )
        elapsed = round(time.time() - t0, 4)
        stats = {
            "n_rows": len(rows),
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": elapsed,
            "write": write_info,
            "qa_shaped_skip": qa_shaped_skip,
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        _best_effort_audit(basename, len(rows))
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        cls = type(e).__name__
        is_parse = "Polars" in cls or "Compute" in cls or "Parse" in cls or "Arrow" in cls
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class="parse_error" if is_parse else "unknown",
            message=str(e),
            stats={"n_rows_partial": len(rows)},
            exc=e,
        )
        return {"source_file": basename, "skipped": False, "ok": False, "error": str(e)}


def _print_verbose(result: dict[str, Any]) -> None:
    basename = result.get("source_file")
    if result.get("skipped"):
        print(f"skip {basename}", file=sys.stderr)
    elif result.get("ok"):
        if result.get("qa_shaped_skip"):
            print(f"skip {basename} (QA-shaped split, 0 rows)", file=sys.stderr)
        else:
            print(
                f"ok {basename} rows={result.get('n_rows')} "
                f"status={result.get('extract_status_counts')}",
                file=sys.stderr,
            )
    else:
        print(f"FAIL {basename}: {result.get('error')}", file=sys.stderr)


def extract_guidelines(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``epfl-llm/guidelines`` files under ``raw_dir``
    into staging shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}`` — a QA-shaped
    (or otherwise unrecognised) file counts toward ``inputs``/``ok`` but
    contributes 0 to ``rows``; it is never counted as ``failed``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    results: list[dict[str, Any]] = []
    t0 = time.time()

    if workers == 1:
        for fp in files:
            result = process_one(fp, processed_dir=processed_dir, force=force)
            results.append(result)
            if verbose:
                _print_verbose(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(process_one, fp, processed_dir=processed_dir, force=force): fp
                for fp in files
            }
            for fut in as_completed(futs):
                result = fut.result()
                results.append(result)
                if verbose:
                    _print_verbose(result)

    ok = sum(1 for r in results if r.get("ok"))
    failed = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    rows = sum(int(r.get("n_rows") or 0) for r in results if r.get("ok"))
    summary = {"inputs": len(files), "ok": ok, "failed": failed, "rows": rows}

    run_id = get_settings().run_id or utc_now_iso().replace(":", "").replace("-", "")
    try:
        write_run_manifest(
            processed_dir,
            SOURCE,
            run_id,
            config={
                "raw_dir": str(raw_dir),
                "max_files": max_files,
                "workers": workers,
                "force": force,
            },
            totals={
                **summary,
                "skipped": sum(1 for r in results if r.get("skipped")),
                "elapsed_sec": round(time.time() - t0, 3),
            },
        )
    except Exception:  # noqa: BLE001 — manifest is best-effort, never fail the run on it
        pass

    return summary


# --------------------------------------------------------------------------- #
# --report: parse-only field-shape table (no shard / marker / manifest / audit)
# --------------------------------------------------------------------------- #

_SAMPLE_MAX = 4
_SAMPLE_TRUNC = 60


def _stringify(value: object) -> str:
    text = str(value)
    if len(text) > _SAMPLE_TRUNC:
        text = text[: _SAMPLE_TRUNC - 3] + "..."
    return text.replace("\n", " ")


def _render_field_shape(rows: list[dict[str, Any]], n_files: int, n_skipped: int) -> str:
    n = len(rows)
    lines = [
        f"field-shape report - source={SOURCE} schema={SCHEMA_VERSION}",
        f"files={n_files}  rows={n}  skipped_qa_or_unrecognised={n_skipped}",
        "",
    ]
    col_w = max((len(c) for c in ARTICLE_COLUMNS), default=6)
    header = f"{'column':<{col_w}}  {'non-null%':>9}  {'distinct':>8}  samples"
    lines.append(header)
    lines.append("-" * len(header))
    for col in ARTICLE_COLUMNS:
        vals = [r.get(col) for r in rows]
        non_null = [v for v in vals if v is not None]
        pct = round(100.0 * len(non_null) / n, 1) if n else 0.0
        distinct = len({repr(v) for v in non_null})
        samples = "; ".join(_stringify(v) for v in non_null[:_SAMPLE_MAX])
        lines.append(f"{col:<{col_w}}  {pct:>9}  {distinct:>8}  {samples}")

    lines += ["", "extract_status histogram:"]
    hist = Counter(str(r.get("extract_status")) for r in rows)
    for status, cnt in sorted(hist.items()):
        lines.append(f"  {status:<10} {cnt}")

    lines += ["", "subset / license breakdown:"]
    sub = Counter(str(r.get("subset")) for r in rows)
    lic = Counter(str(r.get("license")) for r in rows)
    for k, c in sorted(sub.items()):
        lines.append(f"  subset={k:<16} {c}")
    for k, c in sorted(lic.items()):
        lines.append(f"  license={k:<16} {c}")

    return "\n".join(lines) + "\n"


def _run_report(raw_dir: Path, max_files: int = 0) -> int:
    files = discover(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no guidelines *.parquet/*.jsonl under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    n_skipped = 0
    for fp in files:
        try:
            columns = _schema_columns(fp)
            if _is_qa_shaped(columns):
                print(f"  skip {fp.name}: QA-shaped columns {sorted(columns)}", file=sys.stderr)
                n_skipped += 1
                continue
            file_rows = list(iter_rows_from_file(fp))
            if not file_rows:
                print(
                    f"  skip {fp.name}: no recognisable text column {sorted(columns)}",
                    file=sys.stderr,
                )
                n_skipped += 1
                continue
            rows.extend(file_rows)
        except Exception as e:  # noqa: BLE001 — report is best-effort per file
            print(f"  WARN: could not parse {fp.name}: {e}", file=sys.stderr)
    print(_render_field_shape(rows, len(files), n_skipped), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        description="epfl-llm/guidelines clinical guidelines corpus to episteme.articles"
        " staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "guidelines")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered guidelines files (env: EPISTEME_SAMPLE_LIMIT)",
    )
    p.add_argument("--workers", type=int, default=1, help="Parallel file workers")
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true", help="print ok/skip/FAIL per file to stderr")
    p.add_argument(
        "--report",
        action="store_true",
        help="parse only; print a field-shape table; write NO shard/marker/manifest/audit",
    )
    args = p.parse_args(argv)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir

    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    if args.report:
        return _run_report(raw_dir, args.max_files)

    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"raw_dir={raw_dir.resolve()} workers={max(1, args.workers)}")

    res = extract_guidelines(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no guidelines *.parquet/*.jsonl under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
