#!/usr/bin/env python3
"""
Episteme – Europe PMC preprint full-text XML.gz → episteme.articles (v1.1).

Unit of work: one range archive (e.g. PPR1080877_PPR1238855.xml.gz).

  python -m episteme.data.europepmc.preprints.extract_europepmc_preprints \\
    --raw-dir ./01_raw/europepmc/preprints \\
    --processed-dir ./02_processed \\
    --max-files 1 --workers 1
"""

from __future__ import annotations

import argparse
import gzip
import sys
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# TODO(Plan 2): remove this sys.path bootstrap when the extract modules are reworked
_SRC = Path(__file__).resolve().parents[4]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.data.article_schema import (  # noqa: E402
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


def parse_article(art: ET.Element, source_file: str) -> dict[str, Any]:
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
                last = _child_text(el, "surname")
                fore = _child_text(el, "given-names")
                if last or fore:
                    authors.append(f"{fore} {last}".strip() if fore else last)
        elif t in ("license", "license-p") and not license_raw:
            license_raw = _itertext(el)[:500]
            # also href on license
            href = el.get("{http://www.w3.org/1999/xlink}href") or el.get("href")
            if href and href not in license_raw:
                license_raw = f"{href} {license_raw}".strip()

    lic, lic_url, lic_snip = normalize_license(license_raw or None)
    subset = subset_from_license(lic, default="text_mining")

    if pmcid:
        cid = f"pmcid:{pmcid}"
    elif pmid:
        cid = f"pmid:{pmid}"
    elif doi:
        cid = f"doi:{doi}"
    else:
        cid = f"europepmc_preprint:{source_file}:{hash(title) % 10**10}"

    row: dict[str, Any] = {
        "id": cid,
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": pmcid or pmid or doi or None,
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
    }
    return finalize_row(row)


def is_valid_gzip(path: Path) -> bool:
    """True if file starts with gzip magic bytes (1f 8b)."""
    try:
        with path.open("rb") as f:
            magic = f.read(2)
        return magic == b"\x1f\x8b"
    except OSError:
        return False


def iter_articles(path: Path) -> Iterator[dict[str, Any]]:
    if path.name.endswith(".gz"):
        if not is_valid_gzip(path):
            raise OSError(f"corrupt_source: not a gzip file (bad magic): {path.name}")
        opener = gzip.open
    else:
        opener = open
    with opener(path, "rb") as f:
        for _event, elem in ET.iterparse(f, events=("end",)):
            if _local(elem.tag) != "article":
                continue
            try:
                yield parse_article(elem, path.name)
            finally:
                elem.clear()


def process_file(
    path: Path,
    *,
    processed_dir: Path,
    warehouse_root: Path,
    force: bool,
) -> dict[str, Any]:
    basename = path.name
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    subset_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    try:
        if path.name.endswith(".gz") and not is_valid_gzip(path):
            raise OSError(f"corrupt_source: not a gzip file (bad magic): {basename}")

        for row in iter_articles(path):
            status_counts[str(row.get("extract_status"))] += 1
            subset_counts[str(row.get("subset"))] += 1
            rows.append(row)

        write_info = write_rows(
            rows,
            warehouse_root,
            source=SOURCE,
            source_file=basename,
            prefer_parquet=True,
        )
        elapsed = round(time.time() - t0, 3)
        stats = {
            "n_rows": len(rows),
            "extract_status_counts": dict(status_counts),
            "subset_counts": dict(subset_counts),
            "elapsed_sec": elapsed,
            "write": write_info,
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        is_corrupt = (
            "corrupt_source" in msg or "Not a gzipped" in msg or "BadGzipFile" in type(e).__name__
        )
        err_class = "corrupt_source" if is_corrupt else "parse_error"
        try:
            mark_failed(
                processed_dir,
                SOURCE,
                basename,
                error_class=err_class,
                message=msg,
                stats={"n_rows_partial": len(rows)},
                exc=e,
            )
        except Exception as mark_err:  # noqa: BLE001
            print(f"  WARN: could not write failure marker: {mark_err}")
        return {
            "source_file": basename,
            "skipped": False,
            "ok": False,
            "error": msg,
            "error_class": err_class,
        }


def discover(raw_dir: Path) -> list[Path]:
    files = sorted(raw_dir.glob("*.xml.gz")) + sorted(raw_dir.glob("*.xml"))
    files += sorted(raw_dir.rglob("PPR*.xml.gz"))
    seen: set[str] = set()
    out: list[Path] = []
    for f in files:
        if f.is_file() and f.name not in seen:
            seen.add(f.name)
            out.append(f)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="EPMC preprints → episteme.articles")
    p.add_argument("--raw-dir", type=Path, default=Path("./01_raw/europepmc/preprints"))
    p.add_argument("--processed-dir", type=Path, default=Path("./02_processed"))
    p.add_argument("--warehouse-dir", type=Path, default=None)
    p.add_argument("--max-files", type=int, default=1)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    raw_dir = args.raw_dir
    processed_dir = args.processed_dir
    warehouse = args.warehouse_dir or processed_dir
    processed_dir.mkdir(parents=True, exist_ok=True)
    warehouse.mkdir(parents=True, exist_ok=True)

    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    files = discover(raw_dir)
    if not files:
        print(f"ERROR: no preprint xml.gz under {raw_dir}", file=sys.stderr)
        return 1
    if args.max_files > 0:
        files = files[: args.max_files]

    workers = max(1, args.workers)
    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"files={len(files)} workers={workers}")

    results: list[dict[str, Any]] = []
    if workers == 1:
        for fp in files:
            print(f"→ {fp.name} ...")
            res = process_file(
                fp, processed_dir=processed_dir, warehouse_root=warehouse, force=args.force
            )
            results.append(res)
            _print(res)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [
                ex.submit(
                    process_file,
                    fp,
                    processed_dir=processed_dir,
                    warehouse_root=warehouse,
                    force=args.force,
                )
                for fp in files
            ]
            for fut in as_completed(futs):
                res = fut.result()
                results.append(res)
                _print(res)

    run_id = utc_now_iso().replace(":", "").replace("-", "")
    write_run_manifest(
        processed_dir,
        SOURCE,
        run_id,
        config={"max_files": args.max_files, "workers": workers},
        totals={
            "ok": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok") and not r.get("skipped")),
            "skipped": sum(1 for r in results if r.get("skipped")),
        },
    )
    n_fail = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    print(
        f"done ok={sum(1 for r in results if r.get('ok'))} "
        f"skipped={sum(1 for r in results if r.get('skipped'))} failed={n_fail}"
    )
    return 1 if n_fail else 0


def _print(res: dict[str, Any]) -> None:
    if res.get("skipped"):
        print(f"  skip {res.get('source_file')}")
    elif res.get("ok"):
        print(
            f"  ok {res.get('source_file')} rows={res.get('n_rows')} "
            f"status={res.get('extract_status_counts')} subset={res.get('subset_counts')}"
        )
    else:
        print(f"  FAIL {res.get('source_file')}: {res.get('error')}")


if __name__ == "__main__":
    raise SystemExit(main())
