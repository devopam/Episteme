#!/usr/bin/env python3
"""
Episteme – ApolloCorpus → episteme.articles (v1.1).

Unit of work: one JSON / JSONL shard under 01_raw/multilingual/apollo/...

Handles:
  - JSON array of strings or objects
  - JSONL
  - dict with data/records/items list
  - nested text fields

  python -m episteme.data.apollo.extract \\
    --raw-dir ./01_raw/multilingual/apollo \\
    --processed-dir ./02_processed \\
    --max-files 2 --workers 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

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
    utc_now_iso,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "apollo"


def _load_records(path: Path) -> list[Any]:
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return []
    try:
        obj = json.loads(raw)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for k in ("data", "records", "items", "texts", "conversations", "samples"):
                if isinstance(obj.get(k), list):
                    return obj[k]
            return [obj]
    except json.JSONDecodeError:
        pass
    out: list[Any] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append(line)
    return out


def _extract_text(rec: Any) -> str:
    if isinstance(rec, str):
        return rec
    if not isinstance(rec, dict):
        return str(rec)
    for k in ("text", "content", "body", "article", "output", "response", "message"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            inner = _extract_text(v)
            if inner:
                return inner
        if isinstance(v, list):
            # dialogue turns
            parts = []
            for turn in v:
                if isinstance(turn, str):
                    parts.append(turn)
                elif isinstance(turn, dict):
                    parts.append(
                        turn.get("value")
                        or turn.get("content")
                        or turn.get("text")
                        or turn.get("utterance")
                        or ""
                    )
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
    # conversation-style
    if "conversations" in rec and isinstance(rec["conversations"], list):
        return _extract_text({"text": rec["conversations"]})
    return ""


def _extract_lang(rec: Any, source_file: str) -> str | None:
    if isinstance(rec, dict):
        for k in ("lang", "language", "locale", "lang_code"):
            if rec.get(k):
                return str(rec[k])
    # filename hints: en_text.json, zh_...
    name = source_file.lower()
    for code in ("en", "zh", "es", "fr", "de", "ja", "ko", "pt", "it", "ru", "ar", "hi"):
        if f"_{code}_" in name or name.startswith(f"{code}_") or f".{code}." in name:
            return code
    return None


def _extract_id(rec: Any, text: str, source_file: str, idx: int) -> str:
    if isinstance(rec, dict):
        for k in ("id", "uid", "doc_id", "uuid", "sample_id"):
            if rec.get(k) is not None:
                return f"apollo:{rec[k]}"
    h = hashlib.sha1(f"{source_file}:{idx}:{text[:200]}".encode("utf-8")).hexdigest()[:16]
    return f"apollo:{h}"


def record_to_row(rec: Any, source_file: str, idx: int) -> dict[str, Any]:
    text = _extract_text(rec)
    lang = _extract_lang(rec, source_file)
    title = None
    year = None
    doi = None
    if isinstance(rec, dict):
        title = rec.get("title")
        doi = rec.get("doi")
        y = rec.get("year")
        if y is not None:
            try:
                year = int(y)
            except (TypeError, ValueError):
                year = None

    row: dict[str, Any] = {
        "id": _extract_id(rec, text, source_file, idx),
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": str(idx),
        "pmid": None,
        "pmcid": None,
        "doi": doi,
        "title": title,
        "abstract": None,
        "body_text": text or None,
        "authors": None,
        "journal": None,
        "year": year,
        "mesh": None,
        "publication_types": None,
        "language": lang,
        "license": "corpus_declared",
        "license_url": None,
        "license_raw": None,
        "subset": "other",  # until commercial diligence
        "is_retracted": False,
        "pmc_version": None,
        "is_manuscript": None,
        "is_historical_ocr": None,
        "pdf_url": None,
    }
    return finalize_row(row)


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
    rows: list[dict[str, Any]] = []
    try:
        records = _load_records(path)
        for i, rec in enumerate(records):
            row = record_to_row(rec, basename, i)
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
            error_class="parse_error",
            message=str(e),
            stats={"n_rows_partial": len(rows)},
            exc=e,
        )
        return {"source_file": basename, "skipped": False, "ok": False, "error": str(e)}


def discover(raw_dir: Path) -> list[Path]:
    patterns = ["*_text.json", "*.jsonl", "*.json"]
    files: list[Path] = []
    for pat in patterns:
        files.extend(raw_dir.rglob(pat))
    # skip privacy notices / tiny non-data
    out: list[Path] = []
    seen: set[str] = set()
    for f in sorted(files, key=lambda p: (0 if "_text.json" in p.name else 1, str(p))):
        if not f.is_file():
            continue
        if "privacy" in f.name.lower() or "readme" in f.name.lower():
            continue
        # prefer unique full path key for same name in different folders
        key = str(f.relative_to(raw_dir)) if raw_dir in f.parents else f.name
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ApolloCorpus → episteme.articles")
    p.add_argument("--raw-dir", type=Path, default=Path("./01_raw/multilingual/apollo"))
    p.add_argument("--processed-dir", type=Path, default=Path("./02_processed"))
    p.add_argument("--warehouse-dir", type=Path, default=None)
    p.add_argument("--max-files", type=int, default=2, help="0 = all")
    p.add_argument("--workers", type=int, default=2)
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
        print(f"ERROR: no apollo json under {raw_dir}", file=sys.stderr)
        return 1
    if args.max_files > 0:
        files = files[: args.max_files]

    workers = max(1, args.workers)
    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"files={len(files)} workers={workers}")

    results: list[dict[str, Any]] = []
    if workers == 1:
        for fp in files:
            print(f"→ {fp} ...")
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
            f"status={res.get('extract_status_counts')}"
        )
    else:
        print(f"  FAIL {res.get('source_file')}: {res.get('error')}")


if __name__ == "__main__":
    raise SystemExit(main())
