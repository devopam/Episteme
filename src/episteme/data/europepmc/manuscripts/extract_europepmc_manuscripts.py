#!/usr/bin/env python3
"""Extraction: europepmc_manuscript — parse EBI author-manuscript ``.tar.gz``
archives into the unified ``episteme.articles`` row schema (contract v1.4).

EBI ships author manuscripts (deposited under funder text-mining mandates) as
gzipped tar archives, one archive per baseline/increment batch, e.g.
``author_manuscript_txt.PMC00Nxxxxxx.baseline.YYYY-MM-DD.tar.gz``. Unlike
``pubmed``/``pmc``/``europepmc_preprint`` (one *file* = one row), the unit of
work here is one *archive*, and each archive holds many per-manuscript
members: a ``.txt`` (plain body text, no title/abstract structure) and/or a
``.xml`` (JATS) file per manuscript, named by its ``PMC`` id
(e.g. ``PMC0012345.txt`` / ``PMC0012345.xml``).

Per tar member:
  * ``.txt`` — decoded as the entire ``body_text``; no title/abstract split is
    available in this format (``title = abstract = None``).
  * ``.xml`` — JATS. The member's bytes are written to a temp file and parsed
    via the shared ``episteme.data.jats.parse_jats_fields`` helper (JATS
    parsing is NOT reimplemented here).
  * ``pmcid`` — regex ``PMC\\d+`` against the *member* name (the per-manuscript
    filename inside the tar, not the archive's own filename), kept in the full
    ``PMC<digits>`` form (matches ``extract_pmc.py`` / ``jats.py``'s
    convention). For an ``.xml`` member, the JATS-parsed ``pmcid`` is
    preferred when present; the filename-derived id is the fallback (JATS
    metadata is sometimes id-less inside these archives).

Spec §3.2 ``europepmc_manuscript`` row -- the licence/subset are NOT
data-derived:
  * ``license_raw = "text mining / applicable copyright"`` — a fixed string.
    EBI author manuscripts are not openly licensed; they are provided for
    text-mining purposes under a specific EBI agreement, never a CC/permissive
    licence.
  * ``subset = "text_mining"`` is HARDCODED (a literal assignment, not
    ``article_schema.subset_from_license(...)``) — a deliberate spec-mandated
    exception, not an oversight: a manuscript row must NEVER be eligible for
    the ``commercial`` corpus shard, regardless of what
    ``normalize_license``/``subset_from_license`` would infer from that raw
    string, now or after some future change to their classification rules.
    (As it happens, ``normalize_license("text mining / applicable
    copyright")`` already recognises the "TEXT MINING" phrase and returns
    ``license="text_mining"``, and ``subset_from_license("text_mining") ==
    "text_mining"`` too -- confirmed in
    ``tests/data/test_extract_europepmc_manuscript.py::test_manuscript_never_commercial``
    -- so the hardcode agrees with, rather than overrides, the current
    behaviour; it exists purely as insurance against that behaviour changing.)
  * ``is_manuscript = True`` on every row.
  * ``container_id`` / ``book_meta`` = ``None`` on every row (manuscripts are
    not book rows).

``extract_status`` (via ``finalize_row`` / ``decide_extract_status``):
  * ``ok``      — non-empty text (``.txt`` body, or JATS ``<body>``/abstract)
                  at least ``MIN_OK_TEXT_LEN`` chars.
  * ``empty``   — an id was found but no title/abstract/body text.
  * ``dropped`` — no id and no text at all (should not occur in practice: the
                  member-name regex or the ``source_file``/member fallback
                  keeps ``id`` non-empty for every emitted row).
  * unparseable ``.tar.gz`` (or a member that raises while extracting) ->
    ``mark_failed`` records it for the *whole archive*, which is counted
    ``failed`` and emits no rows -- matching ``extract_pubmed``/
    ``extract_europepmc_preprints``'s one-input-file-at-a-time contract, here
    at archive granularity.

Importable core: ``extract_europepmc_manuscripts(raw_dir, processed_dir, *,
max_files=0, force=False, workers=1, verbose=False) ->
{"inputs","ok","failed","rows"}``. ``main()`` is the thin CLI wrapper;
``--report`` parses in memory (honouring ``--max-files``) and prints a
field-shape table without writing any shard, marker, manifest or audit row.

Roadmap CLI (§4.7):
  python -m episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts \\
    --raw-dir ./01_raw/europepmc/manuscripts \\
    --processed-dir ./02_processed \\
    --max-files 20 --workers 4 [--force]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
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

_SRC = Path(__file__).resolve().parents[4]
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
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.jats import parse_jats_fields  # noqa: E402
from episteme.data.staging_writer import write_rows  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "europepmc_manuscript"

# EBI author manuscripts carry no per-record licence field -- this fixed
# string is the licence for every row this extractor emits.
_LICENSE_RAW = "text mining / applicable copyright"

_PMCID_RE = re.compile(r"PMC\d+")

# Serializes the mirror_only file-append across worker threads (mirrors
# extract_pubmed.py / extract_europepmc_preprints.py).
_AUDIT_LOCK = threading.Lock()


def _member_pmcid(member_name: str) -> str | None:
    """``PMC\\d+`` from the tar member's own *filename* (kept full-prefix,
    e.g. ``PMC0000001`` -- matches extract_pmc.py / jats.py's convention).

    Real EBI archives nest members under an accession-range directory, e.g.
    ``PMC001xxxxxx/PMC1249490.xml`` -- matching against the full ``member.name``
    would find ``PMC001`` in that *directory* component first (it too starts
    with ``PMC`` + digits, just followed by literal ``x`` placeholders) and
    silently return the wrong, batch-wide id for every member. Matching only
    the basename avoids that.
    """
    base = member_name.rsplit("/", 1)[-1]
    m = _PMCID_RE.search(base)
    return m.group(0) if m else None


def _row_from_txt(data: bytes, *, source_file: str, member_name: str) -> dict[str, Any]:
    """``.txt`` member: the decoded text IS the body -- no title/abstract
    split is available in this format."""
    # Capped at the same 500_000-char bound jats.parse_jats_fields applies to
    # <body> -- a whole-manuscript plain-text file is, if anything, the
    # branch most likely to need it.
    text = data.decode("utf-8", errors="replace")[:500000]
    pmcid = _member_pmcid(member_name)

    lic, lic_url, lic_snip = normalize_license(_LICENSE_RAW)

    # Archive-format suffix on `id` only (never on `source_record_id`): EBI
    # ships this source as two parallel archive families over the *same*
    # accession ranges (author_manuscript_txt.* / author_manuscript_xml.*,
    # see download_europepmc_manuscript.sh's FMT argument), so the same
    # pmcid can legitimately appear once per format. episteme.articles has
    # no PK/unique constraint on `id` and postgres_loader's idempotency
    # keys on source_file, not `id` -- without this suffix, extracting both
    # format archives for one manuscript would silently produce two rows
    # sharing one `id`.
    row: dict[str, Any] = {
        "id": f"{SOURCE}:{pmcid}:txt" if pmcid else f"{SOURCE}:{source_file}:{member_name}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": pmcid,
        "pmid": None,
        "pmcid": pmcid,
        "doi": None,
        "title": None,
        "abstract": None,
        "body_text": text or None,
        "authors": None,
        "journal": None,
        "year": None,
        "mesh": None,
        "publication_types": None,
        "language": None,
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_snip,
        # HARDCODED, not subset_from_license(lic) -- see module docstring.
        "subset": "text_mining",
        "is_retracted": False,
        "pmc_version": None,
        "is_manuscript": True,
        "is_historical_ocr": None,
        "pdf_url": None,
        "container_id": None,
        "book_meta": None,
    }
    return finalize_row(row)


def _row_from_xml(tmp_path: Path, *, source_file: str, member_name: str) -> dict[str, Any]:
    """``.xml`` member: JATS, parsed via the shared ``jats.parse_jats_fields``
    helper (never reimplemented locally)."""
    fields = parse_jats_fields(tmp_path)
    filename_pmcid = _member_pmcid(member_name)
    # Prefer the JATS-parsed pmcid; fall back to the filename-derived id when
    # the XML's own id is empty (both are already normalised to full "PMC..."
    # by jats.parse_jats_fields / _member_pmcid).
    pmcid = fields.get("pmcid") or filename_pmcid

    lic, lic_url, lic_snip = normalize_license(_LICENSE_RAW)

    # Archive-format suffix on `id` only -- see the matching comment in
    # _row_from_txt: the same pmcid can legitimately appear in both the
    # txt-format and xml-format archive for one manuscript.
    row: dict[str, Any] = {
        "id": f"{SOURCE}:{pmcid}:xml" if pmcid else f"{SOURCE}:{source_file}:{member_name}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": pmcid,
        "pmid": fields.get("pmid"),
        "pmcid": pmcid,
        "doi": fields.get("doi"),
        "title": fields.get("title"),
        "abstract": fields.get("abstract"),
        "body_text": fields.get("body_text"),
        "authors": fields.get("authors"),
        "journal": fields.get("journal"),
        "year": fields.get("year"),
        "mesh": None,
        "publication_types": None,
        "language": fields.get("language"),
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_snip,
        # HARDCODED, not subset_from_license(lic) -- see module docstring.
        "subset": "text_mining",
        "is_retracted": False,
        "pmc_version": None,
        "is_manuscript": True,
        "is_historical_ocr": None,
        "pdf_url": None,
        "container_id": None,
        "book_meta": None,
    }
    return finalize_row(row)


def iter_rows_from_archive(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one row per ``.txt``/``.xml`` member of one manuscript
    ``.tar.gz`` archive. Other member kinds (directories, filelists, etc.) are
    skipped."""
    source_file = path.name
    with tarfile.open(path, "r:gz") as tar:
        members = sorted(
            (m for m in tar.getmembers() if m.isfile()),
            key=lambda m: m.name,
        )
        for member in members:
            lower = member.name.lower()
            if lower.endswith(".txt"):
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                yield _row_from_txt(data, source_file=source_file, member_name=member.name)
            elif lower.endswith(".xml"):
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                tmp = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
                try:
                    tmp.write(data)
                    tmp.close()
                    yield _row_from_xml(
                        Path(tmp.name), source_file=source_file, member_name=member.name
                    )
                finally:
                    os.unlink(tmp.name)
            # else: not a manuscript text/xml member -- skip.


def discover(raw_dir: Path) -> list[Path]:
    """One unit of work per archive."""
    return sorted(p for p in raw_dir.glob("*.tar.gz") if p.is_file())


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """One chained ``extract_commit`` audit row on a fresh connection, degrading
    to the file-only mirror when no DB is reachable. Never raises. Mirrors
    extract_pubmed.py / extract_europepmc_preprints.py verbatim in shape."""
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
        for row in iter_rows_from_archive(path):
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
        cls = type(e).__name__
        is_parse = "Tar" in cls or "Parse" in cls or "XML" in cls or "SyntaxError" in cls
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
        print(
            f"ok {basename} rows={result.get('n_rows')} "
            f"status={result.get('extract_status_counts')}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {basename}: {result.get('error')}", file=sys.stderr)


def extract_europepmc_manuscripts(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``*.tar.gz`` archives under ``raw_dir`` into
    staging shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}`` where ``n``
    (``inputs``) counts *archives*, not manuscript members.
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
    files = discover(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no *.tar.gz under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for fp in files:
        try:
            rows.extend(iter_rows_from_archive(fp))
        except Exception as e:  # noqa: BLE001 — report is best-effort per file
            print(f"  WARN: could not parse {fp.name}: {e}", file=sys.stderr)
    print(_render_field_shape(rows, len(files)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        description="Europe PMC author-manuscript tar.gz archives to episteme.articles"
        " staging shards",
    )
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=settings.raw_root / "europepmc" / "manuscripts",
    )
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered *.tar.gz archives (env: EPISTEME_SAMPLE_LIMIT)",
    )
    p.add_argument("--workers", type=int, default=1, help="Parallel archive workers")
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

    res = extract_europepmc_manuscripts(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no *.tar.gz under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
