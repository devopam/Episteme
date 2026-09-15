#!/usr/bin/env python3
"""Extraction: europepmc_preprint — parse per-ID Europe PMC preprint full-text
JATS (``PPR*.xml``) into the unified ``episteme.articles`` row schema (contract
v1.4).

EBI discontinued the bulk preprint feed (2026-09-08 spike); the downloader now
REST-harvests one ``raw_dir/{PPRid}.xml`` per preprint (see
``download_europepmc_preprints.py``). This extractor therefore discovers
``PPR*.xml`` ONLY — no ``*.xml.gz`` range archives, no ``rglob``.

Unit of work: one ``PPR*.xml`` file = one preprint = one row.

Spec §3.2 ``europepmc_preprint`` row:
  * ``source = "europepmc_preprint"``.
  * ``source_record_id = "PPR{n}"`` — the id, from the file-name stem (preprints
    usually have no pmcid, so the stem is the stable key).
  * JATS ``<body>`` -> ``body_text``; ``doi`` present, ``pmid`` usually absent.
  * ``license`` per-preprint (CC-BY common) via
    ``normalize_license(<raw licence from <permissions>/<license>>)`` then
    ``subset = subset_from_license(code)`` (no ``default=`` override — matches
    the pubmed / apollo extractors; a metadata-only preprint with no licence is
    therefore ``subset="open_metadata"``, not ``"text_mining"``).
  * ``container_id`` / ``book_meta`` = ``None``.

``extract_status`` (via ``finalize_row`` / ``decide_extract_status``):
  * ``ok``    — full-text XML with a non-empty ``<body>``.
  * ``empty`` — metadata-only XML (parsed, has an id, no body).
  * unparseable file -> the parse raises, ``mark_failed`` records it, and the
    file is counted ``failed`` (it emits NO row). A genuine 404 is never written
    to ``raw_dir`` by the downloader, so ``PPR*.xml`` stubs are not expected.

Importable core: ``extract_europepmc_preprints(raw_dir, processed_dir, *,
max_files=0, force=False, workers=1, verbose=False) -> {"inputs","ok","failed",
"rows"}``. ``main()`` is the thin CLI wrapper; ``--report`` parses in memory
(honouring ``--max-files``) and prints a field-shape table without writing any
shard, marker, manifest or audit row.

Roadmap CLI (§4.7):
  python -m episteme.data.europepmc.preprints.extract_europepmc_preprints \\
    --raw-dir ./01_raw/europepmc/preprints \\
    --processed-dir ./02_processed \\
    --max-files 20 --workers 4 [--force]
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET

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
    subset_from_license,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "europepmc_preprint"

# Serializes the mirror_only file-append across worker threads (mirrors
# extract_pubmed.py / extract_apollo.py).
_AUDIT_LOCK = threading.Lock()


def _local(tag: str) -> str:
    return tag.split("}")[-1] if tag else tag


def _child_text(parent: ET.Element, name: str) -> str:
    for c in parent:
        if _local(c.tag) == name:
            return "".join(c.itertext()).strip()
    return ""


def _itertext(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def parse_article(art: ET.Element, source_file: str, stem: str) -> dict[str, Any]:
    """One preprint ``<article>`` element -> a finalized ``episteme.articles`` row.

    ``stem`` is the ``PPR{n}`` file-name stem — it is the ``source_record_id``
    and (absent a DOI) the row ``id``.
    """
    pmid = pmcid = doi = title = abstract = body = journal = language = ""
    year: int | None = None
    authors: list[str] = []
    license_raw = ""

    lang = art.get("{http://www.w3.org/XML/1998/namespace}lang") or art.get("lang")
    if lang:
        language = lang

    for el in art.iter():
        t = _local(el.tag)
        if t == "article-id":
            idt = (el.get("pub-id-type") or "").lower()
            val = (el.text or "").strip()
            if idt in ("pmid", "pubmed"):
                pmid = val
            elif idt == "pmcid":
                pmcid = val if val.upper().startswith("PMC") else f"PMC{val}"
            elif idt == "doi":
                doi = val
        elif t == "article-title" and not title:
            title = _itertext(el)
        elif t == "abstract" and not abstract:
            abstract = _itertext(el)
        elif t == "body" and not body:
            body = _itertext(el)[:500000]
        elif t == "journal-title" and not journal:
            journal = _itertext(el)
        elif t == "year" and year is None:
            try:
                year = int((_itertext(el) or "")[:4])
            except ValueError:
                pass
        elif t == "contrib":
            ctype = el.get("contrib-type")
            if ctype in (None, "author"):
                name_el = None
                for c in el:
                    if _local(c.tag) == "name":
                        name_el = c
                        break
                target = name_el if name_el is not None else el
                last = _child_text(target, "surname")
                fore = _child_text(target, "given-names")
                if last or fore:
                    authors.append(f"{fore} {last}".strip() if fore else last)
        elif t in ("license", "license-p") and not license_raw:
            license_raw = _itertext(el)[:500]
            href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href")
            if href and href not in license_raw:
                license_raw = f"{href} {license_raw}".strip()

    lic, lic_url, lic_snip = normalize_license(license_raw or None)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"doi:{doi}" if doi else f"europepmc_preprint:{stem}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": stem,
        "pmid": pmid or None,
        "pmcid": pmcid or None,
        "doi": doi or None,
        "title": title or None,
        "abstract": abstract or None,
        "body_text": body or None,
        "authors": authors or None,
        "journal": journal or None,
        "year": year,
        "mesh": None,
        "publication_types": None,
        "language": language or None,
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_snip,
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


def iter_articles(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one row per ``<article>`` in a per-ID preprint XML file.

    A preprint file is single-article JATS; if the root is not ``<article>`` and
    carries no ``<article>`` descendant, the root itself is parsed (still yields
    a — most likely metadata-only — row).
    """
    stem = path.stem
    root = ET.parse(path).getroot()
    if _local(root.tag) == "article":
        yield parse_article(root, path.name, stem)
        return
    found = False
    for el in root.iter():
        if _local(el.tag) == "article":
            found = True
            yield parse_article(el, path.name, stem)
    if not found:
        yield parse_article(root, path.name, stem)


def discover(raw_dir: Path) -> list[Path]:
    """Per-ID preprint XML only — ``PPR*.xml`` (bulk range archives are gone)."""
    return sorted(p for p in raw_dir.glob("PPR*.xml") if p.is_file())


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """One chained ``extract_commit`` audit row on a fresh connection, degrading
    to the file-only mirror when no DB is reachable. Never raises."""
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
                pass


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
        for row in iter_articles(path):
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
        is_parse = "Parse" in cls or "XML" in cls or "SyntaxError" in cls
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


def extract_europepmc_preprints(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``PPR*.xml`` under ``raw_dir`` into staging shards +
    ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
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
        print(f"ERROR: no PPR*.xml under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for fp in files:
        try:
            rows.extend(iter_articles(fp))
        except Exception as e:  # noqa: BLE001 — report is best-effort per file
            print(f"  WARN: could not parse {fp.name}: {e}", file=sys.stderr)
    print(_render_field_shape(rows, len(files)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        description="Europe PMC preprint per-ID JATS to episteme.articles staging shards",
    )
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=settings.raw_root / "europepmc" / "preprints",
    )
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered PPR*.xml (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = extract_europepmc_preprints(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no PPR*.xml under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
