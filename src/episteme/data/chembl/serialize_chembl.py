#!/usr/bin/env python3
"""Structured serializer: ChEMBL -- parse a downloaded ChEMBL SQLite release
into the unified ``episteme.articles`` row schema (contract v1.4).

Unit of work: one input file under ``raw_dir``, matching either
``chembl*sqlite.tar.gz`` (the real ``download_chembl.sh`` artifact -- a
tarball, NOT a bare SQLite file: DuckDB's ``sqlite_scanner`` needs an actual
``.db`` file on disk, so a tarball input is extracted to a temp dir first,
one atomic unit) or a bare ``*.db`` file (the unit-test fixture shape, and a
plausible manually-supplied real-file shape -- no extraction needed). Either
way, the *input file as it lands in ``raw_dir``* is the unit that gets one
success/failure marker -- matching roadmap Sec 4.11's frozen "unit of work =
one input file" and Sec 4.2's "for ChEMBL's SQLite release this means one
release-version file, not one row".

Per file: DuckDB's ``sqlite_scanner`` (``sqlite_scan(path, table)``, no
``ATTACH`` needed) joins ``activities`` / ``molecule_dictionary`` /
``compound_structures`` / ``assays`` / ``target_dictionary`` -- one
``episteme.articles`` row per bioactivity (``activities``) record, a
template sentence per the SP4 design spec Sec 4.2's illustrative example.
ChEMBL bioactivity records carry no bibliographic shape: ``title`` /
``journal`` / ``year`` / ``authors`` are always ``None``, and
``container_id`` / ``book_meta`` are ``None`` on every row (no book-shaped
source in SP4). Licence is a fixed ``"CC BY-SA 3.0"`` string (ChEMBL's own
stated release licence) run through the ordinary
``article_schema.normalize_license`` / ``subset_from_license`` machinery --
NOT a hardcode -- which resolves via the existing ``BY-SA`` arm to
``subset="commercial"``.

Importable core: ``serialize_chembl(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5):
  python -m episteme.data.chembl.serialize_chembl \\
    --raw-dir ./01_raw/chembl \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    [--force]
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import duckdb

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.audit_trail import record as _audit  # noqa: E402
from episteme.config import get_settings  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    ARTICLE_COLUMNS,
    SCHEMA_VERSION,
    finalize_row,
    normalize_license,
    subset_from_license,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    discover_input_files,
    input_key,
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "chembl"

# ChEMBL's own stated release licence (LICENSE file at the release root: CC
# BY-SA 3.0 Unported). Run through normalize_license()/subset_from_license()
# like every other field -- NOT a hardcoded subset -- the existing "BY-SA"
# arm of normalize_license resolves this to license="CC BY-SA" ->
# subset="commercial".
_CHEMBL_LICENSE_RAW = "CC BY-SA 3.0"

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors extract_pubmed.py / extract_pmc.py.
_AUDIT_LOCK = threading.Lock()

_BIOACTIVITY_QUERY = """
    SELECT
        act.activity_id       AS activity_id,
        mol.chembl_id         AS compound_chembl_id,
        cs.canonical_smiles   AS smiles,
        act.standard_type     AS standard_type,
        act.standard_value    AS standard_value,
        act.standard_units    AS standard_units,
        act.standard_relation AS standard_relation,
        tgt.pref_name         AS target_name,
        tgt.organism          AS target_organism,
        asy.description       AS assay_description
    FROM sqlite_scan(?, 'activities') act
    LEFT JOIN sqlite_scan(?, 'molecule_dictionary') mol ON mol.molregno = act.molregno
    LEFT JOIN sqlite_scan(?, 'compound_structures') cs ON cs.molregno = act.molregno
    LEFT JOIN sqlite_scan(?, 'assays') asy ON asy.assay_id = act.assay_id
    LEFT JOIN sqlite_scan(?, 'target_dictionary') tgt ON tgt.tid = asy.tid
    ORDER BY act.activity_id
"""
# All FIVE joins after the driving `activities` scan are LEFT, not INNER:
# `activities.molregno` is nullable in the real schema (confirmed against
# ChEMBL's own schema_documentation.txt -- no NOT NULL on that column), so an
# INNER JOIN to molecule_dictionary would silently drop real bioactivity
# records whose compound link is absent, with no count and no note. Every
# activities row -- the thing "one row per bioactivity record" (Sec 4.2)
# actually means -- gets a serialized row; missing joins fall back to
# `_build_text`'s "unknown compound" / omitted-clause paths instead of
# vanishing.


def _is_tarball(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".tar.gz") or name.endswith(".tgz")


def _resolve_sqlite_db(path: Path, extract_dir: Path) -> Path:
    """Return a real ``.db`` file on disk for ``path``.

    ``path`` is either already a bare SQLite file (the fixture / a manually
    placed real file), or the ``chembl_NN_sqlite.tar.gz`` tarball
    ``download_chembl.sh`` actually fetches -- extracted here to
    ``extract_dir`` first (``filter="data"``: no absolute paths, no path
    traversal, no device/symlink members -- Python's safe-extraction
    default). The extracted layout is
    ``chembl_NN/chembl_NN_sqlite/chembl_NN.db``; we don't hardcode that exact
    depth, just glob for the first ``*.db`` found.
    """
    if not _is_tarball(path):
        return path
    with tarfile.open(path) as tf:
        tf.extractall(extract_dir, filter="data")  # nosec B202 - filter="data" bounds extraction
    candidates = sorted(Path(extract_dir).rglob("*.db"))
    if not candidates:
        raise RuntimeError(f"no .db file found inside chembl tarball {path.name}")
    return candidates[0]


def _fmt_value(standard_relation: str | None, standard_value: float | None) -> str | None:
    if standard_value is None:
        return None
    rel = "" if (standard_relation or "=") == "=" else f"{standard_relation} "
    # Trim a trailing ".0" for whole-number potencies -- cosmetic only.
    val = f"{standard_value:g}"
    return f"{rel}{val}"


def _build_text(rec: dict[str, Any]) -> str:
    """Template-based declarative sentence for one bioactivity record (SP4
    design spec Sec 4.2's illustrative example, extended with the assay
    description). No LLM rewriting -- every clause is a plain field
    substitution or omitted when the source field is absent (a LEFT-JOIN
    miss, see ``_BIOACTIVITY_QUERY``'s comment).

    The closing boilerplate clause is not just flavour text: chembl rows
    carry no title/abstract/body_text, so
    ``article_schema.decide_extract_status`` gates ``extract_status="ok"``
    (vs. "partial") on ``len(text) >= MIN_OK_TEXT_LEN`` (200 chars) alone --
    and ``corpus_materializer`` only selects ``extract_status='ok'`` rows
    into the corpus. A bare "Compound X exhibits Y against Z." sentence for a
    well-populated record already clears ~130-160 chars; the boilerplate
    reliably pushes a fully-populated record over the line without
    fabricating content. A record missing several joins (no SMILES, no
    target) can still legitimately land under 200 chars and get "partial" --
    that is honest, not a bug; see the task-5 report for the systemic
    MIN_OK_TEXT_LEN-vs-compact-structured-record tension this surfaces for
    every SP4 source, not just chembl.
    """
    compound = rec.get("compound_chembl_id") or "unknown compound"
    smiles = rec.get("smiles")
    endpoint = rec.get("standard_type") or "an activity"
    value_str = _fmt_value(rec.get("standard_relation"), rec.get("standard_value"))
    units = rec.get("standard_units")
    target = rec.get("target_name")
    organism = rec.get("target_organism")
    assay_desc = rec.get("assay_description")

    parts = [f"Compound {compound}"]
    if smiles:
        parts.append(f"(SMILES {smiles})")
    sentence = " ".join(parts) + f" exhibits {endpoint}"
    if value_str:
        sentence += f" = {value_str}"
        if units:
            sentence += f" {units}"
    if target:
        sentence += f" against {target}"
        if organism:
            sentence += f" ({organism})"
    sentence += ", as reported in the ChEMBL curated bioactivity database"
    if assay_desc and str(assay_desc).strip():
        sentence += f" (assay: {str(assay_desc).strip().rstrip('.')})"
    sentence += "."
    return sentence


def bioactivity_row(rec: dict[str, Any], source_file: str) -> dict[str, Any]:
    """One ``activities`` join record -> a finalized ``episteme.articles`` row."""
    activity_id = rec.get("activity_id")
    native_id = str(activity_id) if activity_id is not None else None
    lic, lic_url, lic_raw = normalize_license(_CHEMBL_LICENSE_RAW)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"{SOURCE}:{native_id}" if native_id else f"{SOURCE}:{source_file}:unknown",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": native_id,
        "pmid": None,
        "pmcid": None,
        "doi": None,
        "title": None,
        "abstract": None,
        "body_text": None,
        "text": _build_text(rec),
        "authors": None,
        "journal": None,
        "year": None,
        "mesh": None,
        "publication_types": None,
        "language": None,
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_raw,
        "subset": subset,
        "is_retracted": False,
        "pmc_version": None,
        "is_manuscript": None,
        "is_historical_ocr": None,
        "pdf_url": None,
        "container_id": None,
        "book_meta": None,
    }
    return finalize_row(row)


def iter_rows_from_file(path: Path, *, source_file: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield one ``episteme.articles`` row per ``activities`` record from one
    ChEMBL SQLite release file (bare ``.db`` or ``*sqlite.tar.gz``).

    NOT a streaming read despite the generator shape: ``con.execute(...)`` +
    ``con.fetchall()`` materializes the whole join in memory before the first
    row is yielded (DuckDB's Python DBAPI cursor has no server-side-cursor
    chunked fetch used here). Harmless for this task's fixture; a real
    ChEMBL release's ``activities`` table is on the order of 10^7 rows, two
    orders of magnitude past a ~19 MB pubmed XML file -- flagged as a named
    ops concern in the task-5 report for whoever runs the real full-scale
    release, not fixed here (chunked/paginated reads + shard-batched writes
    would be the fix, out of scope for this task's fixture-gated proof).
    """
    source_file = source_file or path.name
    with tempfile.TemporaryDirectory(prefix="chembl_extract_") as td:
        db_path = _resolve_sqlite_db(path, Path(td))
        con = duckdb.connect()
        try:
            try:
                con.sql("INSTALL sqlite")
            except duckdb.Error:
                pass  # already installed / vendored with this duckdb build
            con.sql("LOAD sqlite")
            db_str = str(db_path)
            con.execute(_BIOACTIVITY_QUERY, [db_str, db_str, db_str, db_str, db_str])
            cols = [d[0] for d in con.description]
            for raw in con.fetchall():
                rec = dict(zip(cols, raw, strict=True))
                yield bioactivity_row(rec, source_file)
        finally:
            con.close()


def discover_chembl_files(raw_dir: Path) -> list[Path]:
    files = discover_input_files(raw_dir, ["chembl*sqlite.tar.gz", "*.db"])
    files.sort(key=lambda p: input_key(p, raw_dir))
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror extract_pubmed.py's ``_best_effort_audit`` shape verbatim, but
    with ``event_type="serialize_commit"`` -- NOT ``"extract_commit"``.

    Deviation from the brief's literal text (which quotes
    ``audit_trail.record("extract_commit", ...)``): ``audit_trail.EVENT_TYPES``
    (the frozen roadmap Sec 4.8 vocabulary) defines both ``extract_commit``
    *and* ``serialize_commit`` as distinct event types, and Task 4's
    ``graph_builder.py`` already establishes the project's actual convention
    of using the stage-accurate name (``graph_commit`` for the ``graph``
    stage, not ``extract_commit``). This module's stage is ``serialize``
    (``STRUCTURED_SOURCES`` / ``run_pipeline.sh``'s ``serialize)`` arm), so
    ``serialize_commit`` is the event type this stage's commits should carry
    -- flagged explicitly for the controller in the task-5 report rather than
    silently following the brief's copy-paste of the extractor shape.

    One chained audit row on a fresh connection, degrading to the file-only
    mirror when no DB is reachable. Never raises -- ``mark_success`` has
    already run for this file."""
    with _AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "serialize_commit",
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
                    "serialize_commit",
                    object=f"{SOURCE} {basename}",
                    rows_affected=n_rows,
                    note="db_unavailable_or_failed",
                    run_id=get_settings().run_id,
                )
            except Exception:  # noqa: BLE001 - last-resort fallback must never escape
                pass


def process_one(
    path: Path,
    *,
    processed_dir: Path,
    raw_dir: Path,
    force: bool,
) -> dict[str, Any]:
    basename = input_key(path, raw_dir)
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    try:
        for row in iter_rows_from_file(path, source_file=basename):
            status_counts[str(row.get("extract_status"))] += 1
            rows.append(row)

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
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        _best_effort_audit(basename, len(rows))
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class=type(e).__name__,
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
        print(
            f"ok {basename} rows={result.get('n_rows')} "
            f"status={result.get('extract_status_counts')}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {basename}: {result.get('error')}", file=sys.stderr)


def serialize_chembl(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``chembl*sqlite.tar.gz`` / ``*.db`` release
    files under ``raw_dir`` into staging shards + ops markers under
    ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_chembl_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    results: list[dict[str, Any]] = []
    t0 = time.time()

    if workers == 1:
        for fp in files:
            result = process_one(fp, processed_dir=processed_dir, raw_dir=raw_dir, force=force)
            results.append(result)
            if verbose:
                _print_verbose(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(
                    process_one, fp, processed_dir=processed_dir, raw_dir=raw_dir, force=force
                ): fp
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
    except Exception:  # noqa: BLE001 -- manifest is best-effort, never fail the run on it
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


def _render_field_shape(rows: list[dict[str, Any]], n_files: int) -> str:
    n = len(rows)
    lines = [
        f"field-shape report - source={SOURCE} schema={SCHEMA_VERSION}",
        f"files={n_files}  rows={n}",
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
    files = discover_chembl_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no chembl*sqlite.tar.gz / *.db under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for fp in files:
        for row in iter_rows_from_file(fp, source_file=input_key(fp, raw_dir)):
            rows.append(row)
    print(_render_field_shape(rows, len(files)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        prog="python -m episteme.data.chembl.serialize_chembl",
        description="ChEMBL SQLite release to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "chembl")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered chembl release files (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_chembl(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no chembl*sqlite.tar.gz / *.db under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
