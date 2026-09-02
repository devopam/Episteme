#!/usr/bin/env python3
"""
Episteme – PubMed XML → episteme.articles rows (contract v1.1).

Streaming iterparse, per-file restart markers, Parquet (or JSONL fallback).

Example:
  python -m episteme.data.pubmed.extract \\
    --raw-dir ./01_raw/pubmed \\
    --processed-dir ./02_processed \\
    --max-files 1
"""

from __future__ import annotations

import argparse
import gzip
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

# Allow running without install: add src/ to path
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.data.checkpoint_markers import (  # noqa: E402
    is_success,
    list_input_files,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.article_schema import (  # noqa: E402
    SCHEMA_VERSION,
    finalize_row,
    utc_now_iso,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "pubmed"


def _local(tag: str) -> str:
    return tag.split("}")[-1] if tag else tag


def _child_text(parent: ET.Element, name: str) -> str:
    for c in parent:
        if _local(c.tag) == name:
            return "".join(c.itertext()).strip()
    return ""


def parse_pubmed_article(art: ET.Element, source_file: str) -> dict[str, Any]:
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
    is_retracted = False

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
                    elif jn == "PubDate" and year is None:
                        for pc in j:
                            if _local(pc.tag) == "Year":
                                try:
                                    year = int((pc.text or "").strip()[:4])
                                except ValueError:
                                    pass
            elif ln == "Language":
                if not language:
                    language = (c.text or "").strip()
            elif ln == "PublicationTypeList":
                for pt in c:
                    if _local(pt.tag) == "PublicationType":
                        val = "".join(pt.itertext()).strip()
                        if val:
                            pub_types.append(val)
                            if "retract" in val.lower():
                                is_retracted = True

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

    # PubmedData ArticleIds
    for c in art:
        if _local(c.tag) != "PubmedData":
            continue
        for node in c.iter():
            if _local(node.tag) != "ArticleId":
                continue
            id_type = (node.get("IdType") or "").lower()
            val = (node.text or "").strip()
            if id_type == "doi":
                doi = val
            elif id_type == "pmc":
                pmcid = val if val.upper().startswith("PMC") else f"PMC{val}"
            elif id_type == "pubmed" and not pmid:
                pmid = val

    abstract = "\n".join(abstract_parts) if abstract_parts else None
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
        "license": "unknown",
        "license_url": None,
        "license_raw": None,
        "subset": "open_metadata",
        "is_retracted": is_retracted,
        "pmc_version": None,
        "is_manuscript": None,
        "is_historical_ocr": None,
        "pdf_url": None,
    }
    return finalize_row(row)


def iter_articles_from_file(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.name.endswith(".gz") else open
    source_file = path.name
    with opener(path, "rb") as f:
        for _event, elem in ET.iterparse(f, events=("end",)):
            if _local(elem.tag) != "PubmedArticle":
                continue
            try:
                yield parse_pubmed_article(elem, source_file)
            finally:
                elem.clear()


def process_file(
    path: Path,
    *,
    processed_dir: Path,
    warehouse_root: Path,
    force: bool = False,
) -> dict[str, Any]:
    basename = path.name
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []

    try:
        for row in iter_articles_from_file(path):
            status_counts[str(row.get("extract_status"))] += 1
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
            "elapsed_sec": elapsed,
            "write": write_info,
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class="parse_error" if "Parse" in type(e).__name__ else "unknown",
            message=str(e),
            stats={"n_rows_partial": len(rows)},
            exc=e,
        )
        return {
            "source_file": basename,
            "skipped": False,
            "ok": False,
            "error": str(e),
        }


def discover_pubmed_files(raw_dir: Path) -> list[Path]:
    patterns = ["pubmed*.xml.gz", "pubmed*.xml"]
    files = list_input_files(raw_dir, patterns)
    # Prefer baseline-looking paths first
    files.sort(key=lambda p: (0 if "baseline" in str(p).lower() else 1, p.name))
    return files


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PubMed → episteme.articles extractor")
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=Path("./01_raw/pubmed"),
        help="Directory containing pubmed*.xml.gz (searched recursively)",
    )
    p.add_argument(
        "--processed-dir",
        type=Path,
        default=Path("./02_processed"),
        help="Processed root (ops markers + warehouse sibling)",
    )
    p.add_argument(
        "--warehouse-dir",
        type=Path,
        default=None,
        help="Iceberg/Parquet warehouse root (default: <processed-dir>/warehouse)",
    )
    p.add_argument("--max-files", type=int, default=1, help="Max input files (sample mode default 1)")
    p.add_argument("--force", action="store_true", help="Reprocess even if success marker exists")
    p.add_argument(
        "--limit-rows",
        type=int,
        default=0,
        help="Optional cap on rows per file (0 = all); useful for tiny smoke tests",
    )
    args = p.parse_args(argv)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir
    warehouse = args.warehouse_dir or (processed_dir / "warehouse")
    processed_dir.mkdir(parents=True, exist_ok=True)
    warehouse.mkdir(parents=True, exist_ok=True)

    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    files = discover_pubmed_files(raw_dir)
    if not files:
        print(f"ERROR: no pubmed*.xml.gz under {raw_dir}", file=sys.stderr)
        return 1

    files = files[: max(1, args.max_files)]
    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"raw_dir={raw_dir.resolve()}")
    print(f"processed_dir={processed_dir.resolve()}")
    print(f"warehouse={warehouse.resolve()}")
    print(f"files_to_process={len(files)}")

    results = []
    for fp in files:
        print(f"→ {fp.name} ...")
        # optional row limit via wrapper around process — implement inline if needed
        if args.limit_rows and args.limit_rows > 0:
            res = _process_with_row_limit(
                fp,
                processed_dir=processed_dir,
                warehouse_root=warehouse,
                force=args.force,
                limit_rows=args.limit_rows,
            )
        else:
            res = process_file(
                fp,
                processed_dir=processed_dir,
                warehouse_root=warehouse,
                force=args.force,
            )
        results.append(res)
        if res.get("skipped"):
            print(f"  skip ({res.get('reason')})")
        elif res.get("ok"):
            print(
                f"  ok rows={res.get('n_rows')} status={res.get('extract_status_counts')} "
                f"write={res.get('write', {}).get('format')} elapsed={res.get('elapsed_sec')}s"
            )
        else:
            print(f"  FAIL: {res.get('error')}")

    run_id = utc_now_iso().replace(":", "").replace("-", "")
    write_run_manifest(
        processed_dir,
        SOURCE,
        run_id,
        config={
            "raw_dir": str(raw_dir),
            "max_files": args.max_files,
            "force": args.force,
            "limit_rows": args.limit_rows,
        },
        totals={"results": results},
    )

    n_ok = sum(1 for r in results if r.get("ok"))
    n_skip = sum(1 for r in results if r.get("skipped"))
    n_fail = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    print(f"done ok={n_ok} skipped={n_skip} failed={n_fail}")
    return 1 if n_fail else 0


def _process_with_row_limit(
    path: Path,
    *,
    processed_dir: Path,
    warehouse_root: Path,
    force: bool,
    limit_rows: int,
) -> dict[str, Any]:
    basename = path.name
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    try:
        for i, row in enumerate(iter_articles_from_file(path)):
            status_counts[str(row.get("extract_status"))] += 1
            rows.append(row)
            if i + 1 >= limit_rows:
                break
        write_info = write_rows(
            rows,
            warehouse_root,
            source=SOURCE,
            source_file=f"{basename}.limit{limit_rows}",
            prefer_parquet=True,
        )
        # Note: success marker uses real basename only when full file processed.
        # For limit-rows smoke tests, do NOT mark full-file success.
        elapsed = round(time.time() - t0, 3)
        return {
            "source_file": basename,
            "skipped": False,
            "ok": True,
            "n_rows": len(rows),
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": elapsed,
            "write": write_info,
            "smoke_limit_rows": limit_rows,
        }
    except Exception as e:  # noqa: BLE001
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class="unknown",
            message=str(e),
            exc=e,
        )
        return {"source_file": basename, "skipped": False, "ok": False, "error": str(e)}


if __name__ == "__main__":
    raise SystemExit(main())
