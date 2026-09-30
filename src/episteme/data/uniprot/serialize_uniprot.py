#!/usr/bin/env python3
"""Structured serializer: UniProt -- parse a downloaded Swiss-Prot FASTA
release into the unified ``episteme.articles`` row schema (contract v1.4).

Unit of work: one input file under ``raw_dir``, matching either
``*.fasta.gz`` (the real ``download_uniprot.sh`` artifact --
``uniprot_sprot.fasta.gz``, gzip-compressed) or a bare ``*.fasta`` file (the
unit-test fixture shape, and a plausible manually-supplied real-file shape).
Either way, the *input file as it lands in ``raw_dir``* is the unit that gets
one success/failure marker -- matching Task 5's chembl convention and
roadmap Sec 4.11's frozen "unit of work = one input file".

Per file: one ``episteme.articles`` row per FASTA record (``>db|accession|
entry_name description OS=... OX=... GN=... PE=... SV=...`` header + amino
acid sequence lines up to the next ``>`` or EOF). Streamed line-by-line
(NOT loaded whole into memory) -- a real ``uniprot_sprot.fasta.gz``
decompresses to several hundred MB.

Scope ruling (SP4 task-6, settled -- see task-6-report.md): this module
parses FASTA only. UniProt's richer ``.dat`` flat-file format (EMBL-style
line-tagged records carrying function/disease/subcellular-location
annotations FASTA headers lack) is reachable via the existing
``download_uniprot.sh`` wrapper (``uniprot_sprot.dat.gz``) but is NOT parsed
here -- this task's own fixture/test plan is FASTA-shaped
(``tests/fixtures/sp4/uniprot/sample.fasta``), and building a ``.dat``
parser is a materially larger scope than FASTA header-splitting. Flagged as
a documented gap (spec Sec 8 open item 5) for a future task, not attempted
here.

UniProt FASTA records carry no bibliographic shape: ``title`` / ``journal``
/ ``year`` / ``authors`` are always ``None``, and ``container_id`` /
``book_meta`` are ``None`` on every row (no book-shaped source in SP4).

Licence: UniProt's real ``LICENSE`` file (at
``https://ftp.uniprot.org/pub/databases/uniprot/current_release/
knowledgebase/complete/LICENSE``, confirmed by fetching it directly at
implementation time) states a plain CC BY 4.0 grant -- run through the
ordinary ``article_schema.normalize_license`` machinery (NOT a hardcode)
to populate ``license``/``license_url``/``license_raw``. ``subset`` is a
SEPARATE, deliberate hardcode to ``"text_mining"`` -- see
``_UNIPROT_LICENSE_RAW``'s comment and ``fasta_record_row`` below for why.

Malformed-header handling: a header missing the ``db|accession|...`` pipe
structure, or with an empty accession field, raises ``ValueError`` --
propagated up through ``iter_rows_from_file`` and caught by ``process_one``,
which calls ``mark_failed`` for the WHOLE file (per-file granularity,
matching ``extract_pubmed.py``'s ``process_one``). This deliberately does
NOT reproduce the pre-restructure prototype's
(``serialize_structured_sources.py::serialize_uniprot``) blanket
``except Exception: return {"id": "uniprot_unknown", ...}`` fallback, which
would silently emit a fake row into the corpus instead of failing loudly.
A record merely missing an optional marker (``OS=``/``OX=``/``GN=``/``PE=``/
``SV=``) is NOT malformed -- it degrades gracefully via ``kv_data.get(...,
"Unknown ...")`` defaults, same as the prototype's success path.

Importable core: ``serialize_uniprot(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5):
  python -m episteme.data.uniprot.serialize_uniprot \\
    --raw-dir ./01_raw/uniprot \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    [--force]
"""

from __future__ import annotations

import argparse
import gzip
import logging
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
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
    finalize_row,
    normalize_license,
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

_LOG = logging.getLogger(__name__)

SOURCE = "uniprot"

# UniProt's own stated release licence -- confirmed by fetching the real
# LICENSE file directly (https://ftp.uniprot.org/pub/databases/uniprot/
# current_release/knowledgebase/complete/LICENSE) at implementation time:
#   "We have chosen to apply the Creative Commons Attribution 4.0
#   International (CC BY 4.0) License
#   (https://creativecommons.org/licenses/by/4.0/) to all copyrightable
#   parts of our databases."
# Run through normalize_license() like every other field -- NOT a hardcode.
# This resolves to license="CC BY", license_url=the real CC URL above (the
# brief's own illustrative "CC BY-ND" guess did not match the real LICENSE
# text -- confirmed and documented in task-6-report.md; "CC BY" is what the
# real data actually normalizes to).
_UNIPROT_LICENSE_RAW = (
    "Creative Commons Attribution 4.0 International (CC BY 4.0) License "
    "(https://creativecommons.org/licenses/by/4.0/)"
)

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors extract_pubmed.py / serialize_chembl.py.
_AUDIT_LOCK = threading.Lock()

_HEADER_KV_MARKERS = ("OS=", "OX=", "GN=", "PE=", "SV=")


def _parse_header(header: str) -> dict[str, str]:
    """Parse one Swiss-Prot FASTA header, e.g.:

      >sp|P68871|HBB_HUMAN Hemoglobin subunit beta OS=Homo sapiens OX=9606 GN=HBB PE=1 SV=2

    Ported from the pre-restructure prototype's ``serialize_uniprot``
    (``src/episteme/data/curate/serialize_structured_sources.py``, lines
    ~91-130): split on ``|`` for db/accession/entry_name, then strip the
    ``OS=``/``OX=``/``GN=``/``PE=``/``SV=`` key-value markers off the tail in
    reverse canonical order (the header always lists them left-to-right as
    OS, OX, GN, PE, SV, so peeling from the right in reverse order is
    unambiguous).

    Raises ``ValueError`` for a genuinely malformed header (missing the
    ``|`` pipe structure entirely, or an empty accession field) -- this is
    the load-bearing tightening over the prototype, which instead caught
    every exception and returned a fake ``"uniprot_unknown"`` row. Missing
    an OPTIONAL marker (OS=/OX=/GN=/PE=/SV=) is NOT malformed; it degrades
    to an "Unknown ..." default below, matching the prototype's own
    success-path behaviour.
    """
    if not header.startswith(">"):
        raise ValueError(f"malformed FASTA header (missing '>'): {header!r}")

    parts = header[1:].split("|")
    if len(parts) < 3:
        raise ValueError(
            f"malformed UniProt FASTA header (expected db|accession|entry... shape): {header!r}"
        )
    accession = parts[1].strip()
    if not accession:
        raise ValueError(f"malformed UniProt FASTA header (empty accession): {header!r}")

    rest = parts[2].strip()
    if not rest:
        raise ValueError(
            f"malformed UniProt FASTA header (missing entry name/description): {header!r}"
        )

    entry_name, _, desc_and_kv = rest.partition(" ")
    kv_data: dict[str, str] = {}
    current_text = desc_and_kv
    for marker in reversed(_HEADER_KV_MARKERS):
        if marker in current_text:
            current_text, val = current_text.split(marker, 1)
            kv_data[marker[:-1]] = val.strip()

    return {
        "accession": accession,
        "entry_name": entry_name,
        "description": current_text.strip(),
        "organism": kv_data.get("OS", "Unknown organism"),
        "gene": kv_data.get("GN", "Unknown gene"),
    }


def _iter_fasta_records(path: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(header_line, sequence)`` tuples from a Swiss-Prot FASTA file,
    plain or gzip-compressed based on the path's suffix. Streams line-by-line
    -- a real ``uniprot_sprot.fasta.gz`` decompresses to several hundred MB,
    so this must not materialize the whole file in memory (mirrors
    ``extract_pubmed.py``'s streaming XML parser, not chembl's
    whole-join-in-memory DuckDB read)."""
    open_func = gzip.open if path.suffix == ".gz" else open
    header: str | None = None
    seq_parts: list[str] = []
    with open_func(path, "rt", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            if line.startswith(">"):
                if header is not None:
                    yield header, "".join(seq_parts)
                header = line
                seq_parts = []
            else:
                seq_parts.append(line.strip())
        if header is not None:
            yield header, "".join(seq_parts)


def _build_text(entry_name: str, description: str, organism: str, gene: str, sequence: str) -> str:
    """Prototype's template sentence, kept verbatim (SP4 task-6 brief): the
    FASTA sequence itself is embedded in ``text``, so even a short protein
    easily clears ``article_schema.MIN_OK_TEXT_LEN`` (200 chars) -- confirmed
    against the fixture entries in task-6-report.md."""
    return (
        f"Protein {entry_name} ({description}) in organism {organism} "
        f"is encoded by gene {gene}. Its amino acid sequence is: {sequence}"
    )


def fasta_record_row(header: str, sequence: str, source_file: str) -> dict[str, Any]:
    """One FASTA header+sequence pair -> a finalized ``episteme.articles`` row."""
    parsed = _parse_header(header)
    lic, lic_url, lic_raw = normalize_license(_UNIPROT_LICENSE_RAW)

    row: dict[str, Any] = {
        "id": f"{SOURCE}:{parsed['accession']}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": parsed["accession"],
        "pmid": None,
        "pmcid": None,
        "doi": None,
        "title": None,
        "abstract": None,
        "body_text": None,
        "text": _build_text(
            parsed["entry_name"],
            parsed["description"],
            parsed["organism"],
            parsed["gene"],
            sequence,
        ),
        "authors": None,
        "journal": None,
        "year": None,
        "mesh": None,
        "publication_types": None,
        "language": None,
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_raw,
        # HARDCODED, NOT subset_from_license(lic) -- see module docstring.
        # A protein-sequence entry must never be eligible for the
        # `commercial` corpus shard purely because normalize_license()
        # happens to resolve UniProt's real CC BY 4.0 licence text to a
        # commercial-eligible code -- this is the exact interpretation SP4
        # task-6 exists to avoid (mirrors extract_europepmc_manuscripts.py's
        # identical subset="text_mining" hardcode pattern).
        "subset": "text_mining",
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
    """Yield one ``episteme.articles`` row per FASTA record in one Swiss-Prot
    release file (bare ``.fasta`` or ``.fasta.gz``).

    A malformed header raises ``ValueError`` from ``_parse_header`` inside
    this generator -- propagated to the caller (``process_one``), which
    fails the WHOLE file via ``mark_failed`` rather than skip-and-continue.
    See module docstring for the per-file-granularity reasoning.
    """
    source_file = source_file or path.name
    for header, sequence in _iter_fasta_records(path):
        yield fasta_record_row(header, sequence, source_file)


def discover_uniprot_files(raw_dir: Path) -> list[Path]:
    files = discover_input_files(raw_dir, ["*.fasta.gz", "*.fasta"])
    files.sort(key=lambda p: input_key(p, raw_dir))
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror serialize_chembl.py's ``_best_effort_audit`` shape verbatim,
    including its ``event_type="serialize_commit"`` ruling (NOT
    ``"extract_commit"`` -- settled by Tasks 4/5, not re-litigated here).

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


def serialize_uniprot(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``*.fasta.gz`` / ``*.fasta`` Swiss-Prot release
    files under ``raw_dir`` into staging shards + ops markers under
    ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_uniprot_files(raw_dir)
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
    files = discover_uniprot_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no *.fasta.gz / *.fasta under {raw_dir}", file=sys.stderr)
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
        prog="python -m episteme.data.uniprot.serialize_uniprot",
        description="UniProt Swiss-Prot FASTA release to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "uniprot")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered uniprot release files (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_uniprot(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no *.fasta.gz / *.fasta under {raw_dir}", file=sys.stderr)
        return 1

    print(f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']}")
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
