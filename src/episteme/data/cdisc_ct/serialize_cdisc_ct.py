#!/usr/bin/env python3
"""Structured serializer driver: CDISC Controlled Terminology (CT) -- discover
NCI EVS quarterly release files under ``01_raw/cdisc_ct/<package>/<release_date>/
<package>_Terminology.txt`` (design spec sec 4.2), parse each with Task 1's
``ct_parse.parse_ct_file``/``build_rows``, and write staging shards + ops
markers under ``02_processed`` -- the SP7 counterpart of ``serialize_mesh.py``,
whose structure (imports, ``_best_effort_audit``, ``process_one``, the
importable core, ``_run_report``, ``main``) this module follows exactly.

Real layout (design spec sec 4.2, confirmed against Task 1's fixture and the
NCI EVS download convention): one release directory per package per quarter,
named by ISO release date (``YYYY-MM-DD``), each holding a single
tab-separated ``<package>_Terminology.txt`` file in Task 1's 8-column shape.
``PACKAGES`` enumerates the five CDISC CT packages this project tracks
(``SDTM``, ``SEND``, ``ADaM``, ``Define-XML``, ``Protocol`` -- design spec sec
2); each package's releases accumulate independently under its own raw
subtree, so only the NEWEST release directory per package is discovered --
older releases stay on disk (for audit/history) but are not re-serialized.

Restartability (project-wide rule, docs/09-extraction-contract.md): every
input's checkpoint marker and stored ``articles.source_file`` key off
``input_key(path, raw_dir)`` -- here ``<package>__<release_date>__
<package>_Terminology.txt`` (raw-dir-relative path parts joined by ``__``),
so a package's OLD release and its NEW release get distinct keys even though
both files share a basename, and re-running after a new quarterly release
lands processes only the new one (``test_new_release_is_processed``).

Failure handling: ``CTFormatError`` (Task 1's own malformed-file signal) and
any other exception during parse/build/write are treated identically --
``mark_failed``, no shard written, no partial rows committed (spec's own
"restartable/no partial rows" rule, same as every other SP-era structured
serializer).

Licence: identical to Task 1's ``build_rows`` -- NCI EVS's own statement,
governance-overridden to ``public_domain`` -> ``subset="commercial"``.
``build_rows`` owns that logic; this module does not repeat it.

Importable core: ``serialize_cdisc_ct(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5 precedent):
  python -m episteme.data.cdisc_ct.serialize_cdisc_ct \\
    --raw-dir ./01_raw/cdisc_ct \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.audit_trail import record as _audit  # noqa: E402
from episteme.config import get_settings  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    ARTICLE_COLUMNS,
    SCHEMA_VERSION,
    utc_now_iso,
)
from episteme.data.cdisc_ct.ct_parse import (  # noqa: E402
    CTFormatError,  # noqa: F401 -- caught via `except Exception` (a ValueError subclass)
    build_rows,
    parse_ct_file,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    input_key,
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "cdisc_ct"

# The five CDISC CT packages this project tracks (design spec sec 2), in the
# fixed discovery order used both by discover_cdisc_ct_files and (via the
# same iteration) every downstream report/manifest.
PACKAGES: tuple[str, ...] = ("SDTM", "SEND", "ADaM", "Define-XML", "Protocol")

# Release-directory name shape: an ISO date, e.g. "2026-09-25" (design spec
# sec 4.2 -- the NCI EVS quarterly release's own date-stamped directory).
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors serialize_mesh.py / serialize_chembl.py /
# serialize_pubchem.py / serialize_clinvar.py / serialize_reactome.py.
_AUDIT_LOCK = threading.Lock()


def discover_cdisc_ct_files(raw_dir: Path) -> list[Path]:
    """Discover the newest release file per package under ``raw_dir``, in
    ``PACKAGES`` order. A package with no release directories (or none
    matching ``<package>_Terminology.txt``) is simply absent from the
    result -- not an error, since not every package's raw tree need be
    populated for a given run."""
    out: list[Path] = []
    for pkg in PACKAGES:
        base = Path(raw_dir) / pkg
        if not base.is_dir():
            continue
        dates = sorted(d.name for d in base.iterdir() if d.is_dir() and _DATE.match(d.name))
        for date in reversed(dates):
            f = base / date / f"{pkg}_Terminology.txt"
            if f.is_file():
                out.append(f)
                break
    return out


def release_date_of(path: Path) -> str:
    """The release date a discovered file belongs to -- its parent
    directory's name (``<package>/<release_date>/<package>_Terminology.txt``,
    design spec sec 4.2)."""
    return Path(path).parent.name


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror serialize_mesh.py's ``_best_effort_audit`` shape verbatim,
    including the settled ``event_type="serialize_commit"`` ruling (NOT
    ``"extract_commit"`` -- settled by Tasks 4/5/6, not re-litigated here)
    and the SP6 logged double-failure fallback.

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
                _LOG.warning(
                    "audit mirror_only fallback also failed for %s", basename, exc_info=True
                )


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

    package = path.parent.parent.name
    release_date = path.parent.name

    t0 = time.time()
    rows: list[dict[str, Any]] = []
    try:
        codelists, counts = parse_ct_file(path)
        rows = build_rows(
            codelists,
            package=package,
            release_date=release_date,
            source_file=basename,
        )
        status_counts: Counter[str] = Counter(str(r.get("extract_status")) for r in rows)

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
            "skipped_no_id": counts.get("skipped_no_id", 0),
            "orphan_terms": counts.get("orphan_terms", 0),
            "elapsed_sec": elapsed,
            "write": write_info,
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        _best_effort_audit(basename, len(rows))
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001 -- includes CTFormatError (Task 1)
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


def serialize_cdisc_ct(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse the newest CDISC CT release per package under
    ``raw_dir`` into staging shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_cdisc_ct_files(raw_dir)
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
    files = discover_cdisc_ct_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no CDISC CT release files under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for fp in files:
        basename = input_key(fp, raw_dir)
        codelists, _counts = parse_ct_file(fp)
        rows.extend(
            build_rows(
                codelists,
                package=fp.parent.parent.name,
                release_date=release_date_of(fp),
                source_file=basename,
            )
        )
    print(_render_field_shape(rows, len(files)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        prog="python -m episteme.data.cdisc_ct.serialize_cdisc_ct",
        description=(
            "CDISC Controlled Terminology release files "
            "(<package>/<release_date>/<package>_Terminology.txt) to "
            "episteme.articles staging shards"
        ),
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "cdisc_ct")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered packages' newest release (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_cdisc_ct(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no CDISC CT release files under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
