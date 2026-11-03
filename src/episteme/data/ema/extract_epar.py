#!/usr/bin/env python3
"""Extraction: EMA EPAR PDFs into ``episteme.articles`` rows, in bounded parts.

Input: the ``ema_epar_documents`` feed (``<raw dir>/ema_epar_documents.json``) and the PDFs
that ``download_epar`` stored (``<raw dir>/epar/<id>.pdf``). The feed decides which documents
exist (types from ``feeds.toml``, an English PDF URL under the ``sources.env`` prefix, see
``download_epar``); the files on disk only supply their text.

**Buckets.** A document belongs to bucket ``int(id) // Settings.epar_bucket_ids`` (default
8,000 ids, about 500 documents of the three types). The bucket depends on the id alone, so a
document that appears or leaves never moves another one between buckets; with a bucket by
rank it would, and a failed or held bucket would then keep a row that another bucket also
writes. The width is recorded under ``_ops/ema_epar/bucket_scheme.json`` and a run at another
width is refused (``RuntimeError``), as for DailyMed and PMC. A bucket has a manifest (its
documents, with the feed fields the row carries and the size and sha256 of the file), parts
``ema_epar_bNNNN__partNNNNN`` capped by the profile's ``part_rows`` / ``part_bytes``
(``bucket_parts.write_parts``), and a success marker. A bucket whose ``inputs`` equal its
manifest fingerprint is skipped (unless ``--force``); any other is written again from part 1
after its old parts are deleted (the names of loaded parts go to
``retire_pending.d/<bucket key>.json`` first).

**Text.** ``pdf_text`` reads one PDF with ``pypdf``, page by page. A document over
``Settings.epar_max_pages`` or ``epar_max_bytes`` is not read. Each failure is left out of
the bucket, listed in its ``failed_docs`` with a reason, given a failed marker keyed on
``input_key`` of the file, and counted in ``_ops/ema_epar/failed_inputs.json``; the bucket
goes on. The reasons are told apart: ``pdf_parse_error`` (pypdf raised), ``over_limit``,
``unreadable_file`` and ``no_text``. A document with no extractable text (an image-only scan)
is the one failure that keeps a row: ``extract_status = 'empty'`` and no body, so the gate's
empty-text rule sees it as well as the failed-input count.

**Retire.** A document that the feed no longer lists (or no longer lists with a wanted type or
a valid URL) leaves its bucket on the next run: the bucket is written again without it (or
removed with its parts when it was the last), and ``parts_retire`` deletes the rows of the
parts that had been loaded, with an audit row. The decision reads the feed and the earlier
manifests, never the files on disk. A known document whose file is missing holds its bucket
(not rewritten, listed in ``held_buckets``): its row is not dropped for a missing file.
So does a known document that had a row and changed since, when its new file cannot be read:
``process_bucket`` reads such documents first, before it deletes a part, and on a failure
keeps the bucket's parts and marker (as DailyMed holds a bucket); the next run tries again.
A listed document that was never downloaded is skipped and counted as failed
(``not_downloaded``).

**Guards** (refuse before anything is written; ``--force --reason`` overrides them, with a
``force_override`` audit row; a run under a ``_sample`` processed root skips them): the feed
holds as many rows as its ``meta.total_records``, and the share of the previously known
documents that this feed no longer holds does not exceed ``Settings.epar_max_withdrawn_share``
(``EPISTEME_EPAR_MAX_WITHDRAWN_SHARE``, default 0.1; with no earlier state there is no limit).

Importable core: ``extract_epar(raw_dir, processed_dir, *, max_files=0, force=False,
workers=1, ...) -> dict``. ``main()`` is the thin CLI wrapper.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.config import get_settings  # noqa: E402
from episteme.data import bucket_parts  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    SCHEMA_VERSION,
    finalize_row,
    subset_from_license,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    failed_marker_path,
    input_key,
    is_success,
    mark_failed,
    mark_success,
    ops_root,
    success_marker_path,
    write_marker,
    write_run_manifest,
)
from episteme.data.ema import download_epar  # noqa: E402
from episteme.data.parallel import (  # noqa: E402
    default_workers,
    record_first_failure,
    run_per_file,
)
from episteme.data.regulatory.feeds import feeds_record  # noqa: E402
from episteme.data.staging_writer import WRITE_BATCH_ROWS  # noqa: E402
from episteme.ops.profile import get_profile  # noqa: E402
from episteme.ops.retry import TransientUnitError, is_transient  # noqa: E402
from episteme.ops.skip import add_skip_args, check_skip_args  # noqa: E402
from episteme.ops.stop import stage_guard  # noqa: E402

_LOG = logging.getLogger(__name__)
# pypdf warns about every malformed object it repairs; the reasons that matter are raised.
logging.getLogger("pypdf").setLevel(logging.ERROR)

SOURCE = "ema_epar"
# File name the sample gate reads (``gate.FAILED_INPUTS_FILE``; a test keeps them equal).
FAILED_INPUTS_FILE = "failed_inputs.json"
# EMA reuse terms (docs/02-data-sources.md): reuse allowed, commercial and non-commercial, with
# EMA acknowledged; third-party content excluded. No licence of the code vocabulary names these
# terms exactly; "permissive" (reuse with attribution) is the nearest, and license_raw carries
# the real text so the choice is auditable.
LICENSE = "permissive"
LICENSE_RAW = (
    "European Medicines Agency legal notice: reuse of EMA documents is allowed, commercial and "
    "non-commercial, if EMA is acknowledged as the source; third-party content is excluded"
)

_WRITE_BATCH_ROWS = WRITE_BATCH_ROWS
_FAILED_DOCS_LISTED = 100
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class EparScanError(RuntimeError):
    """The feed cannot be trusted or is missing; nothing was written."""


class EparGuardError(EparScanError):
    """A guard refused the run (a short feed, or too many documents would be withdrawn).
    ``--force --reason`` overrides it."""


class EparBucketError(ValueError):
    """A permanent error in the bucket machinery; it fails the bucket."""


class PdfParseError(ValueError):
    """``pypdf`` could not read the file."""


class PdfLimitError(ValueError):
    """The file is over the page or byte cap; it is not read."""


# ---------------------------------------------------------------------------
# Keys and buckets
# ---------------------------------------------------------------------------
def document_key(path: Path, raw_dir: Path) -> str:
    """The input key of one document file: ``input_key`` relative to the raw dir
    (``epar/12.pdf`` gives ``epar__12.pdf``). The markers and the failed lists use it."""
    return input_key(path, raw_dir)


def check_keys(entries: list[dict]) -> None:
    """Refuse two documents that share a key (compared in lower case: a case-insensitive
    disk would make them one file)."""
    seen: dict[str, str] = {}
    for e in entries:
        k = e["key"].lower()
        if k in seen:
            raise EparBucketError(f"documents {seen[k]} and {e['id']} share a key: {e['key']}")
        seen[k] = e["id"]


def bucket_number(doc_id: str, width: int) -> int:
    return int(doc_id) // width


def bucket_key(n: int) -> str:
    return f"{SOURCE}_b{n:04d}"


def part_name(n: int, i: int) -> str:
    return f"{bucket_key(n)}__part{i:05d}"


def check_bucket_scheme(processed_dir: Path, width: int) -> int:
    """Record the bucket width at the first run and refuse a run at another width, before
    anything is written (a different width gives different bucket keys: the old parts and
    rows would stay and the same documents would load again under the new keys)."""
    stored = bucket_parts.check_bucket_scheme(processed_dir, SOURCE, "bucket_ids", width)
    if stored != width:
        raise RuntimeError(
            f"EPAR bucket width is {width} ids but {processed_dir} was built at {stored}. "
            "A different width gives different bucket keys and would load the same documents "
            "twice. Retire the old ema_epar parts first."
        )
    return stored


def _manifest_dir(processed_dir: Path) -> Path:
    return ops_root(processed_dir, SOURCE) / "buckets"


def _manifest_path(processed_dir: Path, n: int) -> Path:
    return _manifest_dir(processed_dir) / f"{bucket_key(n)}.json"


def _manifest_files(processed_dir: Path) -> list[Path]:
    d = _manifest_dir(processed_dir)
    return sorted(d.glob(f"{bucket_key(0)[:-4]}*.json")) if d.is_dir() else []


# ---------------------------------------------------------------------------
# PDF text
# ---------------------------------------------------------------------------
def pdf_text(path: Path, *, max_pages: int | None = None, max_bytes: int | None = None) -> str:
    """The text of a PDF, page by page, joined by a blank line.

    Raises ``PdfLimitError`` when the file is over ``max_bytes`` or has more than
    ``max_pages`` pages (defaults: ``Settings.epar_max_*``), and ``PdfParseError`` when
    ``pypdf`` cannot read it. A PDF with pages and no text gives ``""``: a scan. The
    result has no control characters (PostgreSQL text cannot hold NUL)."""
    from pypdf import PdfReader

    settings = get_settings()
    max_pages = settings.epar_max_pages if max_pages is None else max_pages
    max_bytes = settings.epar_max_bytes if max_bytes is None else max_bytes
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise PdfLimitError(f"{size} bytes is over the cap of {max_bytes} bytes")
    try:
        with open(path, "rb") as fh:
            reader = PdfReader(fh, strict=False)
            if reader.is_encrypted and not reader.decrypt(""):
                raise PdfParseError("the PDF is encrypted")
            n_pages = len(reader.pages)
            if n_pages == 0:
                raise PdfParseError("the PDF has no pages")
            if n_pages > max_pages:
                raise PdfLimitError(f"{n_pages} pages is over the cap of {max_pages} pages")
            parts: list[str] = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
    except (PdfLimitError, PdfParseError):
        raise
    except OSError:
        raise
    except Exception as e:  # noqa: BLE001 - pypdf raises many types on damaged input
        raise PdfParseError(f"{type(e).__name__}: {e}") from e
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    text = _CONTROL_RE.sub(" ", text)
    return text.encode("utf-8", "replace").decode("utf-8")


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------
def _year(value: Any) -> int | None:
    m = re.match(r"^(\d{4})", str(value or ""))
    return int(m.group(1)) if m else None


def row_from_document(entry: dict[str, Any], text: str) -> dict[str, Any]:
    """One document's manifest entry and PDF text -> a finalized articles row. The status
    is set here: ``ok`` with text, ``empty`` without."""
    body = text if text.strip() else None
    row = {
        "id": f"{SOURCE}:{entry['id']}",
        "source": SOURCE,
        "source_file": None,  # the part name, set by write_parts
        "source_record_id": entry.get("product_number") or None,
        "title": entry.get("name") or f"EPAR document {entry['id']}",
        "body_text": body,
        "year": _year(entry.get("first_published")),
        "language": "en",
        "license": LICENSE,
        "license_raw": LICENSE_RAW,
        "subset": subset_from_license(LICENSE),
        "is_retracted": False,
        "pdf_url": entry.get("url"),
        # book_meta is the only metadata column that fits; it makes decide_extract_status
        # treat the row as a book row, so the status is set here.
        "book_meta": json.dumps(
            {
                "doc_type": entry.get("type"),
                "document_id": entry["id"],
                "medicine_name": entry.get("medicine_name"),
                "first_published_date": entry.get("first_published"),
                "last_updated_date": entry.get("last_updated"),
            }
        ),
        "extract_status": "ok" if body else "empty",
    }
    out = finalize_row(row)
    if out["body_text"] != row["body_text"]:
        raise EparBucketError(f"{entry['id']}: finalize_row changed body_text")
    return out


# ---------------------------------------------------------------------------
# Plan: documents -> buckets, against the earlier manifests
# ---------------------------------------------------------------------------
def _entry(doc: dict, raw_dir: Path, processed_dir: Path) -> dict[str, Any]:
    path = download_epar.document_path(raw_dir, doc["id"])
    key = document_key(path, raw_dir)
    size = path.stat().st_size if path.is_file() else None
    sha = None
    if size is not None:
        try:
            payload = json.loads(
                success_marker_path(processed_dir, download_epar.MARKER_SOURCE, key).read_text(
                    encoding="utf-8"
                )
            )
            sha = payload["stats"].get("sha256")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            sha = None
    return {
        "id": doc["id"],
        "key": key,
        "rel": path.relative_to(raw_dir).as_posix(),
        "size": size,
        "sha256": sha,
        "name": doc.get("name"),
        "product_number": doc.get("ema_product_number"),
        "type": doc.get("type"),
        "medicine_name": doc.get("medicine_name"),
        "first_published": doc.get("first_published_date"),
        "last_updated": doc.get("last_updated_date"),
        "url": doc.get("document_url"),
    }


def _fingerprint_row(e: dict[str, Any]) -> list[Any]:
    """Content identity of one document: the feed's update date, the file (size and the
    sha256 the download recorded), and a digest of the feed fields that the row carries."""
    meta = json.dumps(
        [
            e["name"],
            e["product_number"],
            e["type"],
            e["medicine_name"],
            e["first_published"],
            e["url"],
        ]
    )
    return [
        e["id"],
        e["last_updated"],
        e["key"],
        e["size"],
        e["sha256"],
        hashlib.sha256(meta.encode("utf-8")).hexdigest()[:16],
    ]


def _read_manifests(processed_dir: Path) -> dict[int, list[dict]]:
    """``bucket number -> documents`` of the earlier run's manifests (unreadable: skipped)."""
    out: dict[int, list[dict]] = {}
    for p in _manifest_files(processed_dir):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            out[int(payload["bucket"])] = payload["documents"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def _write_manifests(processed_dir: Path, by_bucket: dict[int, list[dict]]) -> dict[int, Path]:
    paths: dict[int, Path] = {}
    for n in sorted(by_bucket):
        entries = by_bucket[n]
        path = _manifest_path(processed_dir, n)
        write_marker(
            path,
            {
                "bucket": n,
                "key": bucket_key(n),
                "documents": entries,
                "fingerprint": [_fingerprint_row(e) for e in entries],
            },
        )
        paths[n] = path
    return paths


def _delete_all_parts(processed_dir: Path, key: str) -> None:
    bucket_parts.delete_all_parts(processed_dir, SOURCE, key)


def _retire_vanished_buckets(processed_dir: Path, live: set[int]) -> int:
    """Remove the buckets that have a manifest but no document in this run (and are not
    held): their parts (the loaded ones go to the retire pending file), markers and manifest."""
    removed = 0
    for p in _manifest_files(processed_dir):
        try:
            n = int(json.loads(p.read_text(encoding="utf-8"))["bucket"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if n in live:
            continue
        success_marker_path(processed_dir, SOURCE, bucket_key(n)).unlink(missing_ok=True)
        _delete_all_parts(processed_dir, bucket_key(n))
        p.unlink(missing_ok=True)
        removed += 1
    return removed


def _audit_override(reason: str, what: str) -> None:
    bucket_parts.audit_override(SOURCE, reason, what)


def _part_caps() -> tuple[int, int]:
    """``(part_rows, part_bytes)`` from the runtime profile (a test seam)."""
    profile = get_profile()
    return profile.part_rows, profile.part_bytes


def _is_sample(processed_dir: Path) -> bool:
    return bucket_parts.is_sample(processed_dir)


def _had_row(processed_dir: Path, doc_key: str) -> bool:
    """Whether the last extract of a document gave it a row: no failed marker, or one for
    ``no_text`` (that document keeps an ``empty`` row)."""
    try:
        payload = json.loads(
            failed_marker_path(processed_dir, SOURCE, doc_key).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("error_class") == "no_text"


def _changed_known_failure(
    entries: list[dict], done_inputs: list, raw_dir: Path, processed_dir: Path
) -> str | None:
    """Why the bucket must be held, or ``None``. A document that the bucket's last run wrote
    as a row, and whose content changed since, is read first, before any part is deleted. If
    it fails (a transient error is raised, not held), rewriting the bucket would drop its row,
    so the bucket keeps its parts and its marker, as DailyMed holds a bucket. The marker's
    inputs no longer match the manifest, so the next run tries again."""
    before = {row[0]: row for row in done_inputs if isinstance(row, list) and row}
    for entry in entries:
        old = before.get(entry["id"])
        if old is None or old == _fingerprint_row(entry) or not _had_row(processed_dir, old[2]):
            continue
        path = raw_dir / entry["rel"]
        try:
            if not path.is_file() or path.stat().st_size != entry["size"]:
                raise OSError("the file changed or went missing since the plan")
            pdf_text(path)
        except Exception as e:  # noqa: BLE001 - a transient error is raised below
            if is_transient(e):
                raise
            return f"{entry['key']} changed and cannot be read ({type(e).__name__}: {e})"
        finally:
            gc.collect()
    return None


# ---------------------------------------------------------------------------
# One bucket
# ---------------------------------------------------------------------------
def process_bucket(
    manifest_path: Path,
    *,
    raw_dir: Path,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    """Extract one bucket (its manifest is ``manifest_path``) as bounded parts.

    Skipped when its marker has the parts format and its ``inputs`` equal the manifest
    fingerprint (unless ``force``). Else every part is deleted and the bucket is written
    again from part 1. A document that fails is left out and recorded; one with no text
    keeps an ``empty`` row and is recorded too."""
    manifest_path = Path(manifest_path)
    key = manifest_path.stem
    t0 = time.time()
    progress: dict[str, Any] = {"part": None, "rows": 0}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = str(manifest["key"])
        n_bucket = int(manifest["bucket"])
        fingerprint = manifest["fingerprint"]
        done = bucket_parts.read_bucket_marker(processed_dir, SOURCE, key)
        if not force:
            if done is not None and done["inputs"] == fingerprint:
                failed_marker_path(processed_dir, SOURCE, key).unlink(missing_ok=True)
                return {
                    "source_file": key,
                    "skipped": True,
                    "reason": "success_marker",
                    "failed_docs_count": int(done.get("failed_docs_count") or 0),
                    "failed_by_reason": dict(done.get("failed_by_reason") or {}),
                }
            if is_success(processed_dir, SOURCE, key):
                _LOG.warning("%s: an input changed since the bucket completed; written again", key)
        raw_dir = Path(raw_dir)
        entries: list[dict] = manifest["documents"]
        check_keys(entries)
        if done is not None:
            why = _changed_known_failure(entries, done["inputs"], raw_dir, processed_dir)
            if why is not None:
                mark_failed(
                    processed_dir,
                    SOURCE,
                    key,
                    error_class="held_bucket",
                    message=f"bucket not rewritten: {why}",
                )
                return {
                    "source_file": key,
                    "skipped": False,
                    "ok": False,
                    "held": True,
                    "error": why,
                }
        success_marker_path(processed_dir, SOURCE, key).unlink(missing_ok=True)
        _delete_all_parts(processed_dir, key)

        cap_rows, cap_bytes = _part_caps()
        failed: list[dict[str, str]] = []
        by_reason: Counter[str] = Counter()

        def _doc_failed(entry: dict, reason: str, exc: BaseException) -> None:
            by_reason[reason] += 1
            if len(failed) < _FAILED_DOCS_LISTED:
                failed.append({"file": entry["key"], "reason": reason, "error": str(exc)})
            mark_failed(
                processed_dir,
                SOURCE,
                entry["key"],
                error_class=reason,
                message=str(exc),
                exc=exc if reason == "unexpected_error" else None,
            )

        def _bucket_rows() -> Iterator[tuple[dict[str, Any], list]]:
            for entry in entries:
                path = raw_dir / entry["rel"]
                try:
                    if not path.is_file() or path.stat().st_size != entry["size"]:
                        raise OSError("the file changed or went missing since the plan")
                    text = pdf_text(path)
                except PdfLimitError as e:
                    _doc_failed(entry, "over_limit", e)
                    continue
                except PdfParseError as e:
                    _doc_failed(entry, "pdf_parse_error", e)
                    continue
                except Exception as e:  # noqa: BLE001
                    if is_transient(e):
                        raise
                    _doc_failed(entry, "unreadable_file", e)
                    continue
                finally:
                    gc.collect()  # pypdf keeps many objects per file; free them before the next
                row = row_from_document(entry, text)
                if row["extract_status"] == "empty":
                    _doc_failed(entry, "no_text", ValueError("the PDF has no extractable text"))
                else:
                    failed_marker_path(processed_dir, SOURCE, entry["key"]).unlink(missing_ok=True)
                yield row, []

        n, n_rows, status_counts = bucket_parts.write_parts(
            _bucket_rows(),
            processed_dir=processed_dir,
            source=SOURCE,
            part_name=lambda i: part_name(n_bucket, i),
            cap_rows=cap_rows,
            cap_bytes=cap_bytes,
            batch_rows=_WRITE_BATCH_ROWS,
            progress=progress,
        )

        stats: dict[str, Any] = {
            "n_rows": n_rows,
            "parts": n,
            "parts_list": [part_name(n_bucket, i) for i in range(1, n + 1)],
            "inputs": fingerprint,
            "documents": len(entries),
            "failed_docs": failed,
            "failed_docs_count": sum(by_reason.values()),
            "failed_by_reason": dict(by_reason),
            "part_rows": cap_rows,
            "part_bytes": cap_bytes,
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": round(time.time() - t0, 4),
        }
        mark_success(processed_dir, SOURCE, key, stats=stats)
        failed_marker_path(processed_dir, SOURCE, key).unlink(missing_ok=True)  # a held mark
        result = {k: v for k, v in stats.items() if k != "inputs"}
        return {"source_file": key, "skipped": False, "ok": True, **result}
    except Exception as e:  # noqa: BLE001
        if is_transient(e):
            raise TransientUnitError(key, repr(e)) from None
        mark_failed(
            processed_dir,
            SOURCE,
            key,
            error_class=type(e).__name__,
            message=str(e),
            stats={"n_rows_partial": progress.get("rows", 0), "failed_part": progress.get("part")},
            exc=e,
        )
        return {"source_file": key, "skipped": False, "ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def _unit_name(result: dict[str, Any]) -> None:
    """A worker crash names the unit by the manifest basename (``ema_epar_bNNNN.json``)."""
    sf = str(result.get("source_file") or "")
    if sf.startswith(f"{SOURCE}_b") and sf.endswith(".json"):
        result["source_file"] = sf[: -len(".json")]


def _print_verbose(result: dict[str, Any]) -> None:
    key = result.get("source_file")
    if result.get("skipped"):
        print(f"skip {key}", file=sys.stderr)
    elif result.get("ok"):
        print(
            f"ok {key} parts={result.get('parts')} rows={result.get('n_rows')} "
            f"failed_docs={result.get('failed_docs_count')}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {key}: {result.get('error')}", file=sys.stderr)


def extract_epar(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
    skip_units: list[str] | None = None,
    skip_reason: str | None = None,
    force_reason: str | None = None,
    types: tuple[str, ...] | None = None,
) -> dict:
    """Importable core: read the feed and PDFs under ``raw_dir`` and extract them by bucket
    into staging parts + ops markers under ``processed_dir``.

    ``max_files`` limits the number of buckets of the run; ``skip_units`` take bucket keys.
    Returns ``{"inputs", "ok", "failed", "held_buckets", "rows", "buckets", "documents",
    "withdrawn_docs", "retired_buckets", "failed_docs_count", "failed_by_reason",
    "not_downloaded", "refused_rows", "duplicate_ids"}``. ``force_reason`` with ``force``
    overrides the guards. Raises ``EparScanError`` when the feed is missing or lists no
    wanted document, ``EparGuardError`` when a guard refuses."""
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    settings = get_settings()
    width = settings.epar_bucket_ids
    override = bool(force and force_reason)
    sample = _is_sample(processed_dir)
    overridden: list[str] = []

    def _guard(message: str) -> None:
        """Refuse with ``message``, unless a sample run or ``--force --reason``."""
        if sample:
            return
        if not override:
            raise EparGuardError(
                f"{message}; nothing was written. Fix the input, or run again with "
                "--force --reason to override (audited)"
            )
        overridden.append(message)

    feed_path = raw_dir / download_epar.FEED_FILE
    if not feed_path.is_file():
        raise EparScanError(f"the feed file {feed_path} is missing (run the ema download first)")
    feed_stats: dict[str, Any] = {}
    docs = download_epar.load_documents(feed_path, types, stats=feed_stats)
    if not docs:
        raise EparScanError(
            f"no EPAR document of the wanted types in {feed_path} "
            f"({feed_stats.get('feed_rows', 0)} rows); nothing was written or retired"
        )
    total = feed_stats.get("total_records")
    if total is not None and feed_stats["feed_rows"] != total:
        _guard(
            f"the feed has {feed_stats['feed_rows']} rows but meta.total_records = {total}; "
            "it may be cut short"
        )
    processed_dir.mkdir(parents=True, exist_ok=True)
    check_bucket_scheme(processed_dir, width)

    # Plan: one entry per document, grouped by bucket, against the earlier manifests.
    t_phase = time.monotonic()
    earlier = _read_manifests(processed_dir)
    known_by_bucket = {n: {e["id"] for e in es} for n, es in earlier.items()}
    entries = [_entry(d, raw_dir, processed_dir) for d in docs]
    check_keys(entries)
    by_bucket: dict[int, list[dict]] = {}
    held: dict[int, str] = {}
    failed_missing: list[dict[str, str]] = []
    for e in entries:
        n = bucket_number(e["id"], width)
        if e["size"] is None:
            if e["id"] in known_by_bucket.get(n, set()):
                held.setdefault(n, f"{e['key']} is missing and its row is known")
            else:
                failed_missing.append({"file": e["key"], "reason": "not_downloaded"})
            continue
        by_bucket.setdefault(n, []).append(e)
    for n in held:
        by_bucket.pop(n, None)
    current_ids = {d["id"] for d in docs}
    known = withdrawn = 0
    for n, ids in known_by_bucket.items():
        if n in held:
            continue
        known += len(ids)
        withdrawn += len(ids - current_ids)
    limit = settings.epar_max_withdrawn_share
    if known and withdrawn / known > limit:
        _guard(
            f"{withdrawn} of {known} known documents ({withdrawn / known:.1%}) are not in the "
            f"feed; the limit is {limit:.1%} (EPISTEME_EPAR_MAX_WITHDRAWN_SHARE)"
        )
    _LOG.info(
        "plan: %d documents, %d buckets, %d withdrawn, %d not downloaded in %.1f s",
        len(docs),
        len(by_bucket),
        withdrawn,
        len(failed_missing),
        time.monotonic() - t_phase,
    )
    if overridden:
        _audit_override(str(force_reason), "; ".join(overridden))
    paths = _write_manifests(processed_dir, by_bucket)
    retired_buckets = _retire_vanished_buckets(processed_dir, set(paths) | set(held))
    for n, why in held.items():
        mark_failed(
            processed_dir,
            SOURCE,
            bucket_key(n),
            error_class="held_bucket",
            message=f"bucket not rewritten: {why}",
        )
    # A never-downloaded document is a failed input (a run on a sample slice is partial on
    # purpose). Its failed marker is keyed on its file.
    n_missing = len(failed_missing)
    if not sample:
        for f in failed_missing:
            mark_failed(
                processed_dir,
                SOURCE,
                f["file"],
                error_class="not_downloaded",
                message="listed in the feed, but its PDF is not in the raw folder",
            )

    keys = bucket_parts.select_buckets(
        SOURCE, [bucket_key(n) for n in sorted(paths)], skip_units, skip_reason
    )
    if max_files and max_files > 0:
        keys = keys[:max_files]
    by_key = {bucket_key(n): p for n, p in paths.items()}
    manifests = [by_key[k] for k in keys]

    workers = max(1, int(workers))
    t0 = time.time()

    def _on_result(result: dict[str, Any]) -> None:
        _unit_name(result)
        if verbose:
            _print_verbose(result)

    results = run_per_file(
        process_bucket,
        manifests,
        workers=workers,
        on_result=_on_result,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        force=force,
    )
    record_first_failure(SOURCE, "extract", results)

    by_reason: Counter[str] = Counter()
    for r in results:
        by_reason.update(r.get("failed_by_reason") or {})
    if n_missing and not sample:
        by_reason["not_downloaded"] += n_missing
    summary = {
        "inputs": len(keys),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok") and not r.get("skipped")) + len(held),
        "held_buckets": sorted(
            [bucket_key(n) for n in held]
            + [str(r["source_file"]) for r in results if r.get("held")]
        ),
        "rows": sum(int(r.get("n_rows") or 0) for r in results if r.get("ok")),
        "buckets": len(keys),
        "documents": len(docs),
        "withdrawn_docs": withdrawn,
        "retired_buckets": retired_buckets,
        "failed_docs_count": sum(by_reason.values()),
        "failed_by_reason": dict(by_reason),
        "not_downloaded": n_missing,
        "refused_rows": int(feed_stats.get("refused_url", 0))
        + int(feed_stats.get("refused_id", 0)),
        "duplicate_ids": int(feed_stats.get("duplicate_ids", 0)),
    }

    run_id = get_settings().run_id or utc_now_iso().replace(":", "").replace("-", "")
    with contextlib.suppress(Exception):  # the manifest is best-effort
        write_run_manifest(
            processed_dir,
            SOURCE,
            run_id,
            config={
                "raw_dir": str(raw_dir),
                "max_files": max_files,
                "workers": workers,
                "force": force,
                "bucket_ids": width,
                # feeds.toml is data, outside the code hash: its values go into the record.
                "feeds": feeds_record(SOURCE),
            },
            totals={
                **summary,
                "skipped": sum(1 for r in results if r.get("skipped")),
                "elapsed_sec": round(time.time() - t0, 3),
            },
        )
    return summary


def _write_failed_inputs(processed_dir: Path, res: dict[str, Any]) -> None:
    """Tell the sample gate how many inputs failed: failed buckets plus failed documents,
    not counting the documents with no text (a scan keeps its ``empty`` row, which the
    empty-text rule sees). Those are told apart as ``no_text``, so the gate reports them
    without letting them hide another failure."""
    no_text = int((res.get("failed_by_reason") or {}).get("no_text") or 0)
    failed = int(res.get("failed") or 0) + int(res.get("failed_docs_count") or 0) - no_text
    bucket_parts.write_failed_inputs(processed_dir, SOURCE, failed, no_text=no_text)


def _main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    p = argparse.ArgumentParser(description="EMA EPAR PDFs to episteme.articles staging shards")
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "ema")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all buckets; N = the first N buckets (env: EPISTEME_SAMPLE_LIMIT)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="worker processes (default: the runtime profile; EPISTEME_EXTRACT_WORKERS overrides)",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--verbose", action="store_true", help="print ok/skip/FAIL per bucket to stderr")
    add_skip_args(p)
    args = p.parse_args(argv)
    check_skip_args(p, args)
    workers = default_workers() if args.workers is None else max(1, args.workers)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir
    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    print(f"schema={SCHEMA_VERSION} source={SOURCE}")
    print(f"raw_dir={raw_dir.resolve()} workers={workers}")

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    old_level = _LOG.level
    _LOG.addHandler(handler)
    _LOG.setLevel(logging.INFO)
    try:
        res = extract_epar(
            raw_dir,
            processed_dir,
            max_files=args.max_files,
            force=args.force,
            workers=workers,
            verbose=args.verbose,
            skip_units=args.skip_unit,
            skip_reason=args.reason,
            force_reason=args.reason,
        )
    except EparScanError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        _LOG.removeHandler(handler)
        _LOG.setLevel(old_level)

    if res["inputs"] == 0 and not args.skip_unit:
        print(f"ERROR: no EPAR documents under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']} "
        f"failed_docs={res['failed_docs_count']} {res['failed_by_reason']}"
    )
    _write_failed_inputs(processed_dir, res)
    return 1 if res["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    with stage_guard(lambda: None, lock_stage="extract", argv=argv, source=SOURCE) as g:
        rc = _main(argv)
        g.exit_code(rc)
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
