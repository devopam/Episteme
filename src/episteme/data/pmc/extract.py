#!/usr/bin/env python3
"""
Episteme – PMC commercial OA (oa_comm) → episteme.articles (contract v1.1).

Unit of work: one metadata JSON (PMC{id}.{ver}.json), optionally paired with XML.

Example:
  python -m episteme.data.pmc.extract \\
    --raw-dir ./01_raw/pmc/oa_comm \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    --workers 4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.data.checkpoint_markers import (  # noqa: E402
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.article_schema import (  # noqa: E402
    SCHEMA_VERSION,
    finalize_row,
    normalize_license,
    subset_from_license,
    utc_now_iso,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "pmc_oa_comm"


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


def parse_jats_fields(xml_path: Path) -> dict[str, Any]:
    """Extract bibliographic + body fields from JATS XML."""
    out: dict[str, Any] = {
        "title": None,
        "abstract": None,
        "body_text": None,
        "authors": None,
        "journal": None,
        "year": None,
        "language": None,
        "pmid": None,
        "pmcid": None,
        "doi": None,
    }
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return out

    lang = root.get("{http://www.w3.org/XML/1998/namespace}lang") or root.get("lang")
    if lang:
        out["language"] = lang

    authors: list[str] = []
    abstract_parts: list[str] = []
    body_parts: list[str] = []

    for el in root.iter():
        t = _local(el.tag)
        if t == "article-title" and not out["title"]:
            out["title"] = _itertext(el)
        elif t == "abstract":
            # skip nested abstracts if already collecting — take full text of each abstract node once
            if el.find(".//abstract") is None or True:
                txt = _itertext(el)
                if txt and txt not in abstract_parts:
                    abstract_parts.append(txt)
        elif t == "body":
            txt = _itertext(el)
            if txt:
                body_parts.append(txt)
        elif t == "journal-title" and not out["journal"]:
            out["journal"] = _itertext(el)
        elif t == "year" and out["year"] is None:
            try:
                out["year"] = int((_itertext(el) or "")[:4])
            except ValueError:
                pass
        elif t == "article-id":
            idt = (el.get("pub-id-type") or "").lower()
            val = (el.text or "").strip()
            if idt in ("pmid", "pubmed") and not out["pmid"]:
                out["pmid"] = val
            elif idt == "pmcid" and not out["pmcid"]:
                out["pmcid"] = val if val.upper().startswith("PMC") else f"PMC{val}"
            elif idt == "doi" and not out["doi"]:
                out["doi"] = val
        elif t == "contrib" and (el.get("contrib-type") in (None, "author")):
            last = _child_text(el, "surname")
            fore = _child_text(el, "given-names")
            if last or fore:
                authors.append(f"{fore} {last}".strip() if fore else last)

    if abstract_parts:
        # Prefer shortest unique abstract block (avoid body-sized duplicates)
        abstract_parts = sorted(set(abstract_parts), key=len)
        out["abstract"] = abstract_parts[0][:50000]
    if body_parts:
        out["body_text"] = body_parts[0][:500000]
    if authors:
        out["authors"] = authors
    return out


def find_xml_for_meta(meta_path: Path, raw_dir: Path, version_id: str) -> Path | None:
    candidates = [
        meta_path.parent.parent / "xml" / f"{version_id}.xml",
        raw_dir / "xml" / f"{version_id}.xml",
        raw_dir / "xml" / "all" / f"{version_id}.xml",
        meta_path.with_suffix(".xml"),
    ]
    # version_id may be PMC123.1 — also try without path tricks
    for c in candidates:
        if c.is_file():
            return c
    # search under raw_dir/xml
    xml_root = raw_dir / "xml"
    if xml_root.is_dir():
        hits = list(xml_root.rglob(f"{version_id}.xml"))
        if hits:
            return hits[0]
    return None


def row_from_meta_and_xml(meta_path: Path, raw_dir: Path) -> dict[str, Any]:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    source_file = meta_path.name
    version_id = meta_path.stem  # PMC13525915.1

    pmcid = meta.get("pmcid") or ""
    if pmcid and not str(pmcid).upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    if not pmcid and version_id.upper().startswith("PMC"):
        pmcid = version_id.split(".")[0]

    license_code = meta.get("license_code") or meta.get("license") or ""
    lic, lic_url, lic_raw = normalize_license(str(license_code) if license_code else None)
    # Prefer structured code from PMC when already short
    if license_code and len(str(license_code)) < 40 and not lic_url:
        lic = str(license_code).strip()
        if lic.upper() in ("CC0", "CC BY", "CC BY-SA", "CC BY-ND"):
            pass
        elif "NC" in lic.upper():
            lic, lic_url, lic_raw = normalize_license(lic)

    subset = subset_from_license(lic, default="commercial")
    # oa_comm should stay commercial only if license allows
    if subset != "commercial" and lic not in ("CC0", "CC BY", "CC BY-SA", "CC BY-ND"):
        subset = subset_from_license(lic, default="text_mining")

    year = None
    cit = meta.get("citation") or ""
    m = re.search(r"\b((?:19|20)\d{2})\b", cit)
    if m:
        year = int(m.group(1))

    title = meta.get("title")
    abstract = meta.get("abstract")
    pmid = str(meta["pmid"]) if meta.get("pmid") is not None else None
    doi = meta.get("doi")

    jats = {}
    xml_path = find_xml_for_meta(meta_path, raw_dir, version_id)
    if xml_path is not None:
        jats = parse_jats_fields(xml_path)
        title = title or jats.get("title")
        abstract = abstract or jats.get("abstract")
        pmid = pmid or jats.get("pmid")
        doi = doi or jats.get("doi")
        if jats.get("pmcid"):
            pmcid = jats["pmcid"]
        if jats.get("year") is not None:
            year = jats["year"]

    row: dict[str, Any] = {
        "id": f"pmcid:{pmcid}" if pmcid else f"pmc:{version_id}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": version_id,
        "pmid": pmid,
        "pmcid": pmcid or None,
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "body_text": jats.get("body_text"),
        "authors": jats.get("authors"),
        "journal": jats.get("journal"),
        "year": year,
        "mesh": None,
        "publication_types": None,
        "language": jats.get("language"),
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_raw,
        "subset": subset if subset == "commercial" else subset,
        "is_retracted": bool(meta.get("is_retracted", False)),
        "pmc_version": str(meta.get("version")) if meta.get("version") is not None else (
            version_id.split(".")[-1] if "." in version_id else None
        ),
        "is_manuscript": meta.get("is_manuscript"),
        "is_historical_ocr": meta.get("is_historical_ocr"),
        "pdf_url": None,
    }
    # pdf_url from s3-style to https if present
    pdf = meta.get("pdf_url")
    if pdf:
        if str(pdf).startswith("s3://pmc-oa-opendata/"):
            row["pdf_url"] = "https://pmc-oa-opendata.s3.amazonaws.com/" + str(pdf).split(
                "s3://pmc-oa-opendata/", 1
            )[-1].split("?")[0]
        else:
            row["pdf_url"] = str(pdf).split("?")[0]

    # Force commercial bucket only when license is commercial-class
    if row["license"] in ("CC0", "CC BY", "CC BY-SA", "CC BY-ND"):
        row["subset"] = "commercial"
    elif str(row["license"]).startswith("CC BY-NC") or row["license"] == "text_mining":
        row["subset"] = "text_mining"

    return finalize_row(row)


def discover_meta_files(raw_dir: Path) -> list[Path]:
    files: list[Path] = []
    meta_dir = raw_dir / "metadata"
    if meta_dir.is_dir():
        files.extend(meta_dir.rglob("PMC*.json"))
    files.extend(raw_dir.rglob("PMC*.json"))
    # unique by name
    seen: set[str] = set()
    out: list[Path] = []
    for f in sorted(files, key=lambda p: p.name):
        if not f.is_file():
            continue
        if f.name in ("sync_meta.txt",):
            continue
        if f.name in seen:
            continue
        seen.add(f.name)
        out.append(f)
    return out


def process_one(
    meta_path: Path,
    *,
    raw_dir: Path,
    processed_dir: Path,
    warehouse_root: Path,
    force: bool,
) -> dict[str, Any]:
    basename = meta_path.name
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    try:
        row = row_from_meta_and_xml(meta_path, raw_dir)
        write_info = write_rows(
            [row],
            warehouse_root,
            source=SOURCE,
            source_file=basename,
            prefer_parquet=True,
        )
        elapsed = round(time.time() - t0, 4)
        stats = {
            "n_rows": 1,
            "extract_status": row.get("extract_status"),
            "subset": row.get("subset"),
            "license": row.get("license"),
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
            error_class="parse_error",
            message=str(e),
            exc=e,
        )
        return {"source_file": basename, "skipped": False, "ok": False, "error": str(e)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PMC oa_comm → episteme.articles")
    p.add_argument("--raw-dir", type=Path, default=Path("./01_raw/pmc/oa_comm"))
    p.add_argument("--processed-dir", type=Path, default=Path("./02_processed"))
    p.add_argument("--warehouse-dir", type=Path, default=None)
    p.add_argument("--max-files", type=int, default=0, help="0 = all discovered metadata JSON files")
    p.add_argument("--workers", type=int, default=1, help="Parallel file workers")
    p.add_argument("--force", action="store_true")
    args = p.parse_args(argv)

    raw_dir = args.raw_dir
    processed_dir = args.processed_dir
    warehouse = args.warehouse_dir or (processed_dir / "warehouse")
    processed_dir.mkdir(parents=True, exist_ok=True)
    warehouse.mkdir(parents=True, exist_ok=True)

    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    files = discover_meta_files(raw_dir)
    if not files:
        print(f"ERROR: no PMC*.json under {raw_dir}", file=sys.stderr)
        return 1

    if args.max_files and args.max_files > 0:
        files = files[: args.max_files]

    workers = max(1, args.workers)
    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"raw_dir={raw_dir.resolve()}")
    print(f"files={len(files)} workers={workers}")

    results: list[dict[str, Any]] = []
    t0 = time.time()

    if workers == 1:
        for fp in files:
            print(f"→ {fp.name} ...")
            res = process_one(
                fp,
                raw_dir=raw_dir,
                processed_dir=processed_dir,
                warehouse_root=warehouse,
                force=args.force,
            )
            results.append(res)
            _print_res(res)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(
                    process_one,
                    fp,
                    raw_dir=raw_dir,
                    processed_dir=processed_dir,
                    warehouse_root=warehouse,
                    force=args.force,
                ): fp
                for fp in files
            }
            for fut in as_completed(futs):
                res = fut.result()
                results.append(res)
                _print_res(res)

    run_id = utc_now_iso().replace(":", "").replace("-", "")
    write_run_manifest(
        processed_dir,
        SOURCE,
        run_id,
        config={
            "raw_dir": str(raw_dir),
            "max_files": args.max_files,
            "workers": workers,
            "force": args.force,
        },
        totals={"results_summary": _summarize(results), "elapsed_sec": round(time.time() - t0, 3)},
    )
    s = _summarize(results)
    print(f"done ok={s['ok']} skipped={s['skipped']} failed={s['failed']}")
    return 1 if s["failed"] else 0


def _print_res(res: dict[str, Any]) -> None:
    if res.get("skipped"):
        print(f"  skip {res.get('source_file')}")
    elif res.get("ok"):
        print(
            f"  ok {res.get('source_file')} status={res.get('extract_status')} "
            f"subset={res.get('subset')} license={res.get('license')}"
        )
    else:
        print(f"  FAIL {res.get('source_file')}: {res.get('error')}")


def _summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "ok": sum(1 for r in results if r.get("ok")),
        "skipped": sum(1 for r in results if r.get("skipped")),
        "failed": sum(1 for r in results if not r.get("ok") and not r.get("skipped")),
    }


if __name__ == "__main__":
    raise SystemExit(main())
