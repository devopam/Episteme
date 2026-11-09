#!/usr/bin/env python3
"""Extraction: europepmc_preprint — EBI's bulk preprint files into the unified
``episteme.articles`` row schema (contract v1.4), in bounded parts.

Input: the files ``PPR<a>_PPR<b>.xml.gz`` that ``download_europepmc_preprints``
stores under ``<raw>/europepmc/preprints_bulk/``. Each file holds one
``<articles>`` root with many ``<article article-type="preprint">`` JATS records
(about 10,000 in the first file).

**Unit.** One bulk file is one unit. Its key is ``input_key(path, raw_dir)``
without the ``.xml.gz`` suffix (for a file directly under ``raw_dir``:
``PPR7001_PPR208218``). The unit has a success marker and parts
``<key>__partNNNNN`` capped by the profile's ``part_rows`` / ``part_bytes``
(``bucket_parts.write_parts``). The unit is read as a stream: ``iterparse`` hands
over one record at a time and the record is cleared after it is parsed, so the
memory use is one record, never the file.

**Resume.** The unit marker holds a fingerprint of the file (size and sha256). A
unit whose marker ``inputs`` equal the fingerprint is skipped (unless
``--force``); any other unit is written again from part 1 after its old parts
are deleted. The names of parts that were loaded go to
``retire_pending.d/<key>.json`` first; ``retire`` (run by ``load`` after a good
load) deletes their rows.

**Row.** ``source = "europepmc_preprint"``; ``source_record_id`` is the PPR id;
``id`` is ``doi:<doi>`` when the record has a DOI, else
``europepmc_preprint:<PPR id>`` (the form of the earlier per-id extractor, so a
loaded row is replaced, not duplicated). ``license`` is
``normalize_license(<first licence element: text and xlink:href>)`` and
``subset = subset_from_license(license)``; CC BY-NC and CC BY-NC-ND are
``text_mining``.

**Skipped records** (counted in the unit marker and in the summary, never
fatal): ``skipped_no_ppr_id`` (no ``article-id`` whose value is ``PPR<n>``),
``skipped_no_text`` (neither body nor abstract text) and ``skipped_duplicate_id``
(the id is already in this unit; the first record stays).

Importable core: ``extract_europepmc_preprints(raw_dir, processed_dir, *,
max_files=0, force=False, workers=1, verbose=False) -> dict``.

  python -m episteme.data.europepmc.preprints.extract_europepmc_preprints \\
    --raw-dir ./01_raw/europepmc/preprints_bulk --processed-dir ./02_processed
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET

_SRC = Path(__file__).resolve().parents[4]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.config import get_settings  # noqa: E402
from episteme.data import bucket_parts  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    ARTICLE_COLUMNS,
    SCHEMA_VERSION,
    finalize_row,
    normalize_license,
    subset_from_license,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    input_key,
    is_success,
    mark_failed,
    mark_success,
    success_marker_path,
    write_run_manifest,
)
from episteme.data.parallel import (  # noqa: E402
    default_workers,
    record_first_failure,
    run_per_file,
)
from episteme.data.staging_writer import WRITE_BATCH_ROWS  # noqa: E402
from episteme.ops.profile import get_profile  # noqa: E402
from episteme.ops.retry import TransientUnitError, is_transient  # noqa: E402
from episteme.ops.skip import add_skip_args, check_skip_args  # noqa: E402
from episteme.ops.stop import stage_guard  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "europepmc_preprint"
BULK_PATTERN = "PPR*_PPR*.xml.gz"
BULK_NAME_RE = re.compile(r"^PPR\d+_PPR\d+\.xml\.gz$")
_SUFFIX = ".xml.gz"
_PPR_RE = re.compile(r"^PPR\d+$")
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_BODY_MAX_CHARS = 500000
_SKIP_KEYS = ("skipped_no_ppr_id", "skipped_no_text", "skipped_duplicate_id")

# Batch size is module-level (read at call time) so tests can shrink it.
_WRITE_BATCH_ROWS = WRITE_BATCH_ROWS


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


def parse_article(art: ET.Element, source_file: str) -> dict[str, Any] | str:
    """One preprint ``<article>`` element -> a finalized row, or the name of the
    skip reason (``"no_ppr_id"``, ``"no_text"``)."""
    pmid = pmcid = doi = title = abstract = body = journal = language = ppr = ""
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
            if _PPR_RE.match(val):
                ppr = ppr or val
            elif idt in ("pmid", "pubmed"):
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
            body = _itertext(el)[:_BODY_MAX_CHARS]
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
        elif t in ("license", "license-p"):
            href = el.get(_XLINK_HREF) or el.get("href")
            if not license_raw:
                license_raw = _itertext(el)[:500]
                if href and href not in license_raw:
                    license_raw = f"{href} {license_raw}".strip()
            elif href and "://" not in license_raw:
                # The first element named no URL; a nested one may.
                license_raw = f"{href} {license_raw}".strip()

    if not ppr:
        return "no_ppr_id"
    if not body and not abstract:
        return "no_text"

    lic, lic_url, lic_snip = normalize_license(license_raw or None)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"doi:{doi}" if doi else f"{SOURCE}:{ppr}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": ppr,
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


def iter_records(path: Path, skips: Counter[str] | None = None) -> Iterator[dict[str, Any]]:
    """Yield one row per usable ``<article>`` of one bulk file, as a stream.

    Memory: ``iterparse`` builds one record at a time; the record and the root's
    children are cleared after it is parsed. A record that is skipped is counted
    in ``skips`` as ``skipped_<reason>``. A repeated id in the file is counted as
    ``skipped_duplicate_id``."""
    skips = skips if skips is not None else Counter()
    seen: set[str] = set()
    depth = 0
    root = None
    with gzip.open(path, "rb") as fh:
        for event, el in ET.iterparse(fh, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                if _local(el.tag) == "article":
                    depth += 1
                continue
            if _local(el.tag) != "article":
                continue
            depth -= 1
            if depth > 0:  # an article inside an article: part of the outer record
                continue
            res = parse_article(el, Path(path).name)
            el.clear()
            if root is not None:
                root.clear()
            if isinstance(res, str):
                skips[f"skipped_{res}"] += 1
                continue
            if res["id"] in seen:
                skips["skipped_duplicate_id"] += 1
                continue
            seen.add(res["id"])
            yield res


def discover(raw_dir: Path) -> list[Path]:
    """The bulk files ``PPR<a>_PPR<b>.xml.gz`` directly under ``raw_dir`` (a flat
    glob, non-recursive: the downloader writes nowhere else), in order of the
    first id."""

    def first_id(p: Path) -> int:
        return int(p.name.split("_", 1)[0][3:])

    return sorted(
        (p for p in Path(raw_dir).glob(BULK_PATTERN) if p.is_file() and BULK_NAME_RE.match(p.name)),
        key=first_id,
    )


def unit_key(path: Path, raw_dir: Path) -> str:
    """The unit key: ``input_key`` without the ``.xml.gz`` suffix."""
    k = input_key(path, raw_dir)
    return k[: -len(_SUFFIX)] if k.endswith(_SUFFIX) else k


def part_name(key: str, n: int) -> str:
    return f"{key}__part{n:05d}"


def fingerprint(path: Path) -> list[str]:
    """``["size:<bytes>", "sha256:<hex>"]`` of the file, read in 1 MiB blocks."""
    h = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
            size += len(block)
    return [f"size:{size}", f"sha256:{h.hexdigest()}"]


def _part_caps() -> tuple[int, int]:
    """``(part_rows, part_bytes)`` from the runtime profile (a test seam)."""
    profile = get_profile()
    return profile.part_rows, profile.part_bytes


def process_unit(
    path: Path,
    *,
    raw_dir: Path,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    """Extract one bulk file as bounded parts; skip it when its marker matches."""
    key = unit_key(path, raw_dir)
    t0 = time.time()
    progress: dict[str, Any] = {"part": None, "rows": 0}
    try:
        fp = fingerprint(path)
        if not force:
            done = bucket_parts.read_bucket_marker(processed_dir, SOURCE, key)
            if done is not None and done["inputs"] == fp:
                return {
                    "source_file": key,
                    "skipped": True,
                    "reason": "success_marker",
                    **{k: int(done.get(k) or 0) for k in _SKIP_KEYS},
                }
            if is_success(processed_dir, SOURCE, key):
                _LOG.warning("%s: input or marker changed; writing the unit again", key)
        # The unit marker goes first: a stop during the rewrite leaves no marker
        # with an old parts_list.
        success_marker_path(processed_dir, SOURCE, key).unlink(missing_ok=True)
        bucket_parts.delete_all_parts(processed_dir, SOURCE, key)

        cap_rows, cap_bytes = _part_caps()
        skips: Counter[str] = Counter()
        rows_src = ((row, []) for row in iter_records(path, skips))
        n_parts, n_rows, status_counts = bucket_parts.write_parts(
            rows_src,
            processed_dir=processed_dir,
            source=SOURCE,
            part_name=lambda n: part_name(key, n),
            cap_rows=cap_rows,
            cap_bytes=cap_bytes,
            batch_rows=_WRITE_BATCH_ROWS,
            progress=progress,
        )
        stats: dict[str, Any] = {
            "n_rows": n_rows,
            "parts": n_parts,
            "parts_list": [part_name(key, i) for i in range(1, n_parts + 1)],
            "inputs": fp,
            **{k: int(skips.get(k, 0)) for k in _SKIP_KEYS},
            "part_rows": cap_rows,
            "part_bytes": cap_bytes,
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": round(time.time() - t0, 4),
        }
        mark_success(processed_dir, SOURCE, key, stats=stats)
        result = {k: v for k, v in stats.items() if k != "inputs"}
        return {"source_file": key, "skipped": False, "ok": True, **result}
    except Exception as e:  # noqa: BLE001
        if is_transient(e):
            raise TransientUnitError(key, repr(e)) from None
        cls = type(e).__name__
        is_parse = "Parse" in cls or "XML" in cls or "SyntaxError" in cls or "BadGzip" in cls
        mark_failed(
            processed_dir,
            SOURCE,
            key,
            error_class="parse_error" if is_parse else cls,
            message=str(e),
            stats={"n_rows_partial": progress.get("rows", 0), "failed_part": progress.get("part")},
            exc=e,
        )
        return {"source_file": key, "skipped": False, "ok": False, "error": str(e)}


def _print_verbose(result: dict[str, Any]) -> None:
    key = result.get("source_file")
    if result.get("skipped"):
        print(f"skip {key}", file=sys.stderr)
    elif result.get("ok"):
        skipped = {k: result.get(k) for k in _SKIP_KEYS}
        print(
            f"ok {key} parts={result.get('parts')} rows={result.get('n_rows')} {skipped}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {key}: {result.get('error')}", file=sys.stderr)


def extract_europepmc_preprints(
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
    """Importable core: extract the bulk files under ``raw_dir`` into staging
    parts + ops markers under ``processed_dir``.

    ``max_files`` limits the number of bulk files; ``skip_units`` take unit keys.
    Returns ``{"inputs", "ok", "failed", "rows", "skipped_no_ppr_id",
    "skipped_no_text", "skipped_duplicate_id"}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    by_key = {unit_key(p, raw_dir): p for p in discover(raw_dir)}
    keys = bucket_parts.select_buckets(SOURCE, list(by_key), skip_units, skip_reason)
    if max_files and max_files > 0:
        keys = keys[:max_files]
    files = [by_key[k] for k in keys]

    workers = max(1, int(workers))
    t0 = time.time()

    results = run_per_file(
        process_unit,
        files,
        workers=workers,
        on_result=_print_verbose if verbose else None,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        force=force,
    )
    record_first_failure(SOURCE, "extract", results)

    ok = sum(1 for r in results if r.get("ok"))
    failed = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    rows = sum(int(r.get("n_rows") or 0) for r in results if r.get("ok"))
    summary: dict[str, Any] = {"inputs": len(files), "ok": ok, "failed": failed, "rows": rows}
    for k in _SKIP_KEYS:
        summary[k] = sum(int(r.get(k) or 0) for r in results)

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
_REPORT_ROWS_PER_FILE = 1000


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
        print(f"ERROR: no {BULK_PATTERN} under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for fp in files:
        gen = iter_records(fp)
        try:
            for _ in range(_REPORT_ROWS_PER_FILE):
                row = next(gen, None)
                if row is None:
                    break
                rows.append(row)
        except Exception as e:  # noqa: BLE001 — report is best-effort per file
            print(f"  WARN: could not parse {fp.name}: {e}", file=sys.stderr)
        finally:
            gen.close()
    print(_render_field_shape(rows, len(files)), end="")
    return 0


def _main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        description="Europe PMC preprint bulk JATS files to episteme.articles staging parts",
    )
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=settings.raw_root / "europepmc" / "preprints_bulk",
    )
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all bulk files; N = the first N (env: EPISTEME_SAMPLE_LIMIT)",
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
    if args.report and args.skip_unit:
        p.error("--skip-unit cannot be used with --report")
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

    res = extract_europepmc_preprints(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=workers,
        verbose=args.verbose,
        skip_units=args.skip_unit,
        skip_reason=args.reason,
    )

    # inputs == 0 after --skip-unit dropped every file is a clean run (R8).
    if res["inputs"] == 0 and not args.skip_unit:
        print(f"ERROR: no {BULK_PATTERN} under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']} "
        f"skipped_no_ppr_id={res['skipped_no_ppr_id']} skipped_no_text={res['skipped_no_text']} "
        f"skipped_duplicate_id={res['skipped_duplicate_id']}"
    )
    bucket_parts.write_failed_inputs(processed_dir, SOURCE, int(res["failed"]))
    return 1 if res["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    # Extract/serialize hold no long DB statement: only the stop handlers and
    # the parent watchdog apply (conn_ref returns None).
    with stage_guard(lambda: None, lock_stage="extract", argv=argv, source=SOURCE) as g:
        rc = _main(argv)
        g.exit_code(rc)
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
