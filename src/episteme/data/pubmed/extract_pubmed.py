#!/usr/bin/env python3
"""Extraction: PubMed — parse the baseline/update ``PubmedArticleSet`` XML
(gzipped ``pubmed*.xml.gz``) into the unified ``episteme.articles`` row schema
(contract v1.4).

Unit of work: one input file. Per file: streaming ``iterparse`` over every
``</PubmedArticle>``, one ``article_schema`` row per record, then
``staging_writer.write_rows``, then ``checkpoint_markers.mark_success`` /
``mark_failed``, then a best-effort chained ``audit_trail`` row.

PubMed XML is *not* JATS — ``jats.py`` is not used here. PubMed records carry no
licence, so every row is ``license="unknown"`` / ``subset="open_metadata"``, and
``container_id`` / ``book_meta`` are ``None`` on every row.

Importable core: ``extract_pubmed(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (§4.7):
  python -m episteme.data.pubmed.extract_pubmed \\
    --raw-dir ./01_raw/pubmed \\
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
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import iterparse as _safe_iterparse

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
    success_marker_path,
    write_run_manifest,
)
from episteme.data.graph_files import GraphFileWriter, marker_graph_info  # noqa: E402
from episteme.data.gz import open_gz  # noqa: E402
from episteme.data.parallel import (  # noqa: E402
    default_workers,
    record_first_failure,
    run_per_file,
    skip_files,
)
from episteme.data.pubmed.relations import (  # noqa: E402
    extract_relations,
    own_article_ids,
    retracted,
)
from episteme.data.staging_writer import write_rows  # noqa: E402
from episteme.ops.retry import TransientUnitError, is_transient  # noqa: E402
from episteme.ops.skip import add_skip_args, check_skip_args  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "pubmed"

# Guards the in-process (workers=1) path only. Across worker processes, the mirror
# append lock in audit_trail and the DB advisory lock serialise audit writes.
_AUDIT_LOCK = threading.Lock()


def _local(tag: str) -> str:
    return tag.split("}")[-1] if tag else tag


def _child_text(parent: Element, name: str) -> str:
    for c in parent:
        if _local(c.tag) == name:
            return "".join(c.itertext()).strip()
    return ""


def parse_pubmed_record(
    art: Element, source_file: str
) -> tuple[dict[str, Any], dict[str, list[dict]], dict[str, int]]:
    """One ``<PubmedArticle>`` -> ``(article row, graph edges, counters)``.

    Lifted from the retired ``extract.py`` (streaming ``_local``/``_child_text``
    walk); the year path additionally falls back to the first 4 digits of
    ``<MedlineDate>``, and ``container_id`` / ``book_meta`` are pinned ``None``.
    """
    pmid = ""
    pmcid = ""
    doi = ""
    title = ""
    abstract_parts: list[str] = []
    authors: list[str] = []
    journal = ""
    year: int | None = None
    mesh: list[str] = []
    pub_types: list[str] = []
    language = ""

    medline = None
    for c in art:
        if _local(c.tag) == "MedlineCitation":
            medline = c
            break

    # PMID
    if medline is not None:
        for c in medline:
            if _local(c.tag) == "PMID":
                pmid = (c.text or "").strip()
                break

    # Article block
    article = None
    if medline is not None:
        for c in medline:
            if _local(c.tag) == "Article":
                article = c
                break

    if article is not None:
        for c in article:
            ln = _local(c.tag)
            if ln == "ArticleTitle":
                title = "".join(c.itertext()).strip()
            elif ln == "Abstract":
                for a in c:
                    if _local(a.tag) == "AbstractText":
                        label = a.get("Label")
                        part = "".join(a.itertext()).strip()
                        if label:
                            abstract_parts.append(f"{label}: {part}")
                        elif part:
                            abstract_parts.append(part)
            elif ln == "AuthorList":
                for auth in c:
                    if _local(auth.tag) != "Author":
                        continue
                    last = _child_text(auth, "LastName")
                    fore = _child_text(auth, "ForeName") or _child_text(auth, "Initials")
                    collective = _child_text(auth, "CollectiveName")
                    if collective:
                        authors.append(collective)
                    elif last or fore:
                        authors.append(f"{fore} {last}".strip() if fore else last)
            elif ln == "Journal":
                for j in c.iter():
                    jn = _local(j.tag)
                    if jn == "Title" and not journal:
                        journal = "".join(j.itertext()).strip()
                    elif jn == "ISOAbbreviation" and not journal:
                        journal = (j.text or "").strip()
                    elif jn == "Year" and year is None:
                        try:
                            year = int((j.text or "").strip()[:4])
                        except ValueError:
                            pass
                    elif jn == "MedlineDate" and year is None:
                        m = re.search(r"\d{4}", j.text or "")
                        if m:
                            year = int(m.group(0))
            elif ln == "Language":
                if not language:
                    language = (c.text or "").strip()
            elif ln == "PublicationTypeList":
                for pt in c:
                    if _local(pt.tag) == "PublicationType":
                        val = "".join(pt.itertext()).strip()
                        if val:
                            pub_types.append(val)

    # Mesh
    if medline is not None:
        for c in medline:
            if _local(c.tag) != "MeshHeadingList":
                continue
            for mh in c:
                if _local(mh.tag) != "MeshHeading":
                    continue
                for d in mh:
                    if _local(d.tag) == "DescriptorName":
                        val = "".join(d.itertext()).strip()
                        if val:
                            mesh.append(val)

    # The record's OWN ids only (a cited reference's ArticleIdList is ignored).
    ids = own_article_ids(art)
    doi = ids.get("doi", "")
    pmc = ids.get("pmc", "")
    pmcid = pmc if not pmc or pmc.upper().startswith("PMC") else f"PMC{pmc}"
    if not pmid:
        pmid = ids.get("pubmed", "")
    edges, counters = extract_relations(art, pmid, source_file)
    is_retracted = retracted(pub_types, edges["comments"])

    abstract = "\n".join(abstract_parts) if abstract_parts else None
    # PubMed records carry no licence text -> unknown / open_metadata.
    lic, lic_url, lic_raw = normalize_license(None)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"pmid:{pmid}" if pmid else f"pubmed:{source_file}:unknown",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": pmid or None,
        "pmid": pmid or None,
        "pmcid": pmcid or None,
        "doi": doi or None,
        "title": title or None,
        "abstract": abstract,
        "body_text": None,
        "authors": authors or None,
        "journal": journal or None,
        "year": year,
        "mesh": mesh or None,
        "publication_types": pub_types or None,
        "language": language or None,
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_raw,
        "subset": subset,
        "is_retracted": is_retracted,
        "pmc_version": None,
        "is_manuscript": None,
        "is_historical_ocr": None,
        "pdf_url": None,
        "container_id": None,
        "book_meta": None,
    }
    return finalize_row(row), edges, counters


def parse_pubmed_article(art: Element, source_file: str) -> dict[str, Any]:
    """The article row only; see ``parse_pubmed_record``."""
    return parse_pubmed_record(art, source_file)[0]


def iter_records_from_file(path: Path, *, source_file: str):
    """Like ``iter_rows_from_file`` but yields ``(row, edges, counters)``."""
    opener = open_gz if path.name.endswith(".gz") else open
    with opener(path, "rb") as f:
        it = _safe_iterparse(f, events=("start", "end"))
        _, root = next(it)
        for event, elem in it:
            if event != "end" or _local(elem.tag) != "PubmedArticle":
                continue
            try:
                yield parse_pubmed_record(elem, source_file)
            finally:
                elem.clear()
                root.clear()


def iter_rows_from_file(path: Path, *, source_file: str | None = None) -> Iterator[dict[str, Any]]:
    """Stream ``<PubmedArticle>`` rows from one ``pubmed*.xml(.gz)`` file.

    ``events=("start", "end")`` so the root ``<PubmedArticleSet>`` can be
    cleared after every record — otherwise it retains a reference to every
    drained child and memory grows for the whole (19 MB+) file.

    ``source_file`` defaults to ``path.name`` when omitted (existing
    positional callers keep working), but callers with a ``raw_dir`` in scope
    must pass ``input_key(path, raw_dir)`` explicitly -- two input files
    sharing a basename in different subdirectories under ``raw_dir`` must not
    collapse to the same ``source_file`` value on the rows themselves.
    """
    for rec in iter_records_from_file(path, source_file=source_file or path.name):
        yield rec[0]


def discover_pubmed_files(raw_dir: Path) -> list[Path]:
    """``pubmed*.xml(.gz)`` files under ``raw_dir``, deduped by resolved full
    path (never by basename) via ``checkpoint_markers.discover_input_files``.

    pubmed's real download layout (``scripts/data/pubmed/download_pubmed.sh``,
    the actual downloader -- ``download_pubmed.py`` is a stub) is NOT flat: it
    writes into ``<raw_dir>/baseline/<file>`` and ``<raw_dir>/updates/<file>``
    (``resolve_dest pubmed`` == ``raw_root/pubmed``, matching this module's
    own ``--raw-dir`` default). So ``input_key`` diverges from the old
    ``path.name`` for every real file post-migration (e.g.
    ``baseline__pubmed24n0001.xml.gz``), not just a hypothetical -- existing
    markers/shards under a live ``processed_root`` are orphaned by this
    change (see the Task 11 report's "operational impact" section, mirroring
    Task 10's bookshelf finding). Nothing in ``list_input_files``'s
    basename-only dedup (the pre-migration behaviour) enforced any particular
    layout either way: two files sharing a basename in different
    subdirectories would have silently collapsed to one discovered file. This
    mirrors ``extract_pmc.discover_meta_files`` / ``extract_bookshelf.
    discover_bookshelf_files`` (Tasks 9/10).

    The baseline-before-updates ordering is preserved and given priority over
    the ``input_key`` tiebreak: baseline files establish PMID records that a
    later "update" file's <PubmedArticle> for the same PMID is meant to
    revise, so baseline must sort (and, when run single-threaded, process)
    first regardless of path-derived identity.
    """
    files = discover_input_files(raw_dir, ["pubmed*.xml.gz", "pubmed*.xml"])
    # Prefer baseline-looking paths first; input_key (not raw path/name) as
    # the tiebreak, consistent with the marker/source_file identity below.
    files.sort(key=lambda p: (0 if "baseline" in str(p).lower() else 1, input_key(p, raw_dir)))
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror extract_pmc.py: one chained ``extract_commit`` audit row on a
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


def _has_graph_info(processed_dir: Path, basename: str) -> bool:
    try:
        return marker_graph_info(processed_dir, SOURCE, basename) is not None
    except ValueError:  # truncated marker: treat as absent and redo the file
        return False


def process_one(
    path: Path,
    *,
    raw_dir: Path,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    # input_key, not path.name: two pubmed*.xml(.gz) files sharing a basename
    # in different subdirectories under raw_dir (not how download_pubmed.py
    # lays files out today, but not something the old basename-keyed dedup
    # enforced either -- see discover_pubmed_files) must not collapse to the
    # same marker/source_file identity.
    basename = input_key(path, raw_dir)
    # A marker without graph_relations (pre-1.6 shard) is redone in full.
    if (
        not force
        and is_success(processed_dir, SOURCE, basename)
        and _has_graph_info(processed_dir, basename)
    ):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}
    # Drop any old marker first: a crash mid-file must never leave a stale success claim.
    success_marker_path(processed_dir, SOURCE, basename).unlink(missing_ok=True)

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    try:
        with GraphFileWriter(processed_dir, SOURCE, basename) as gw:
            for row, edges, rec_counters in iter_records_from_file(path, source_file=basename):
                status_counts[str(row.get("extract_status"))] += 1
                rows.append(row)
                counters.update(rec_counters)
                for rel_name, rel_rows in edges.items():
                    if rel_rows:
                        gw.add(rel_name, rel_rows)
            graph_info = gw.close()

        write_info = write_rows(
            rows, processed_dir, source=SOURCE, source_file=basename, prefer_parquet=True
        )
        elapsed = round(time.time() - t0, 4)
        stats = {
            "n_rows": len(rows),
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": elapsed,
            "write": write_info,
            **graph_info,
            "orcid_dropped": counters["orcid_dropped"],
            "edges_skipped_no_pmid": counters["edges_skipped_no_pmid"],
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        _best_effort_audit(basename, len(rows))
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        if is_transient(e):
            raise TransientUnitError(basename, repr(e)) from None
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class="parse_error" if "Parse" in type(e).__name__ else "unknown",
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


def extract_pubmed(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
    skip_units: list[str] | None = None,
    skip_reason: str | None = None,
) -> dict:
    """Importable core: parse ``pubmed*.xml(.gz)`` under ``raw_dir`` into staging
    shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_pubmed_files(raw_dir)
    files = skip_files(SOURCE, "extract", files, skip_units, skip_reason)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    t0 = time.time()

    results = run_per_file(
        process_one,
        files,
        workers=workers,
        on_result=_print_verbose if verbose else None,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        force=force,
    )
    record_first_failure(SOURCE, "extract", files, results, raw_dir)

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
    files = discover_pubmed_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no pubmed*.xml(.gz) under {raw_dir}", file=sys.stderr)
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
        description="PubMed XML to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "pubmed")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered pubmed*.xml.gz (env: EPISTEME_SAMPLE_LIMIT)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="worker processes (default: the runtime profile; EPISTEME_EXTRACT_WORKERS overrides)",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true", help="print ok/skip/FAIL per file to stderr")
    p.add_argument(
        "--report",
        action="store_true",
        help="parse only; print a field-shape table; write NO shard/marker/manifest/audit",
    )
    add_skip_args(p)
    args = p.parse_args(argv)
    check_skip_args(p, args)
    workers = default_workers() if args.workers is None else max(1, args.workers)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir

    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    if args.report:
        return _run_report(raw_dir, args.max_files)

    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"raw_dir={raw_dir.resolve()} workers={workers}")

    res = extract_pubmed(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=workers,
        verbose=args.verbose,
        skip_units=args.skip_unit,
        skip_reason=args.reason,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no pubmed*.xml.gz under {raw_dir}", file=sys.stderr)
        return 1

    print(f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']}")
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
