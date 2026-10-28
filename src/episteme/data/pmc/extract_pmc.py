#!/usr/bin/env python3
"""Extraction: PMC — parse the OA commercial subset (JATS XML + metadata JSON)
into the unified ``episteme.articles`` row schema (contract v1.1+).

Unit of work: one fixed PMC id bucket (``episteme.data.pmc.buckets``: ids
``b * BUCKET_IDS`` .. ``(b + 1) * BUCKET_IDS - 1``). Discovery walks
``raw_dir`` once, keeps the highest version of each article
(``PMC{id}.{ver}.json``) and writes one manifest per bucket under
``_ops/pmc/buckets/``. ``process_bucket`` parses each article (metadata
JSON, optionally paired with its JATS XML) into one ``article_schema`` row
and streams the rows into bounded parts ``pmc_bNNNN__partNNNNN``. Each part
is capped by the profile's ``part_rows`` / ``part_bytes``; a row that would
take a part over ``part_bytes`` starts the next part, and a single row over
the cap is alone in its part. Each part gets a shard, an ``extract_commit``
audit row, then a success marker; after the last part the bucket gets its
own success marker (``parts``, ``parts_list``, ``inputs`` = the manifest
fingerprint). A bucket whose marker ``inputs`` equal the fingerprint is
skipped (unless ``--force``); any other bucket is written again from part
1, after its old parts are deleted (the names of loaded parts go to the
retire pending file of the bucket,
``retire_pending.d/<bucket key>.json``, first). An article whose parse fails is left out of its
part, listed in the bucket's ``failed_articles`` and given a failed marker;
the bucket still completes.

Importable core: ``extract_pmc(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper.

Roadmap CLI (§4.7):
  python -m episteme.data.pmc.extract_pmc \\
    --raw-dir ./01_raw/pmc/oa_comm \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    --workers 4 \\
    [--force]
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import logging
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.audit_trail import record as _audit  # noqa: E402
from episteme.config import get_settings  # noqa: E402
from episteme.data import parts_retire, staging_writer  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    SCHEMA_VERSION,
    finalize_row,
    normalize_license,
    subset_from_license,
    utc_now_iso,
)
from episteme.data.checkpoint_markers import (  # noqa: E402
    discover_input_files,
    failed_marker_path,
    input_key,
    is_success,
    load_success_marker_path,
    mark_failed,
    mark_success,
    success_marker_path,
    write_run_manifest,
)
from episteme.data.jats import parse_jats_fields  # noqa: E402
from episteme.data.parallel import (  # noqa: E402
    default_workers,
    record_first_failure,
    run_per_file,
)
from episteme.data.pmc.buckets import (  # noqa: E402
    bucket_key,
    group_by_bucket,
    manifest_files,
    part_name,
    read_manifest,
    write_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402
from episteme.ops.profile import PART_ROW_OVERHEAD_BYTES, get_profile  # noqa: E402
from episteme.ops.retry import TransientUnitError, is_transient  # noqa: E402
from episteme.ops.skip import add_skip_args, check_skip_args  # noqa: E402
from episteme.ops.stop import stage_guard  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "pmc"

# Guards the in-process (workers=1) path only. Across worker processes, the mirror
# append lock in audit_trail and the DB advisory lock serialise audit writes.
_AUDIT_LOCK = threading.Lock()

# Batch size is module-level (read at call time) so tests can shrink it.
_WRITE_BATCH_ROWS = staging_writer.WRITE_BATCH_ROWS
# A bucket's stats list at most this many failed articles (plus a count).
_FAILED_ARTICLES_LISTED = 100


def find_xml_for_meta(
    meta_path: Path, raw_dir: Path, version_id: str, *, search: bool = True
) -> Path | None:
    """Locate the JATS XML for one metadata file, most specific match first.

    Order matters: two different metadata files can share the same
    ``version_id`` (e.g. re-downloaded/re-batched inputs), so a shared
    ``raw_dir/xml`` (or ``raw_dir/xml/all``) directory must never be checked
    before the XML colocated with -- or scoped to the same batch as -- the
    metadata file itself, or both metadata files would silently attach the
    same shared XML.

      (a) colocated with the metadata file (``meta_path.with_suffix(".xml")``)
      (b) this batch's own ``xml/`` folder (``meta_path.parent.parent / "xml"``)
      (c) the shared ``raw_dir/xml`` and ``raw_dir/xml/all`` directories
      (d) an ``raw_dir/xml`` rglob fallback -- only when it finds EXACTLY ONE
          match; two or more is an unresolvable ambiguity, logged as a
          warning naming the version_id, and ``None`` is returned rather than
          silently picking one.

    For the normal single-download layout (``download_pmc.py`` writes
    ``metadata/`` and ``xml/`` side by side under one ``raw_dir``), (a) is
    absent and (b)/(c) resolve to the same file, so behaviour is unchanged.

    ``search=False`` skips (d). ``process_bucket`` and
    ``buckets.bucket_fingerprint`` pass it: on real data ``raw_dir/xml`` holds
    about 935k files, too many to walk once per article.
    """
    candidates = [
        meta_path.with_suffix(".xml"),
        meta_path.parent.parent / "xml" / f"{version_id}.xml",
        raw_dir / "xml" / f"{version_id}.xml",
        raw_dir / "xml" / "all" / f"{version_id}.xml",
    ]
    for c in candidates:
        if c.is_file():
            return c
    if not search:
        return None
    # search under raw_dir/xml -- only an unambiguous single match is usable.
    xml_root = raw_dir / "xml"
    if xml_root.is_dir():
        hits = list(xml_root.rglob(f"{version_id}.xml"))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            _LOG.warning(
                "ambiguous XML match for version_id=%s under %s: %d candidates, skipping",
                version_id,
                xml_root,
                len(hits),
            )
    return None


def row_from_meta_and_xml(meta_path: Path, raw_dir: Path, *, search: bool = True) -> dict[str, Any]:
    """One metadata file (and its JATS XML, if found) -> one finalized row.

    ``search`` goes to ``find_xml_for_meta``. ``source_file`` is the metadata
    file's input key; ``process_bucket`` replaces it with the part name."""
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # input_key, not meta_path.name: two metadata files sharing a basename in
    # different subdirectories under raw_dir must not collapse to the same
    # source_file value (see discover_meta_files / process_one below).
    source_file = input_key(meta_path, raw_dir)
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
    xml_path = find_xml_for_meta(meta_path, raw_dir, version_id, search=search)
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
        "subset": subset,
        "is_retracted": bool(meta.get("is_retracted", False)),
        "pmc_version": str(meta.get("version"))
        if meta.get("version") is not None
        else (version_id.split(".")[-1] if "." in version_id else None),
        "is_manuscript": meta.get("is_manuscript"),
        "is_historical_ocr": meta.get("is_historical_ocr"),
        "pdf_url": None,
    }
    # pdf_url from s3-style to https if present
    pdf = meta.get("pdf_url")
    if pdf:
        if str(pdf).startswith("s3://pmc-oa-opendata/"):
            row["pdf_url"] = (
                "https://pmc-oa-opendata.s3.amazonaws.com/"
                + str(pdf).split("s3://pmc-oa-opendata/", 1)[-1].split("?")[0]
            )
        else:
            row["pdf_url"] = str(pdf).split("?")[0]

    # Force commercial bucket only when license is commercial-class
    if row["license"] in ("CC0", "CC BY", "CC BY-SA", "CC BY-ND"):
        row["subset"] = "commercial"
    elif str(row["license"]).startswith("CC BY-NC") or row["license"] == "text_mining":
        row["subset"] = "text_mining"

    return finalize_row(row)


def discover_meta_files(raw_dir: Path) -> list[Path]:
    """``PMC*.json`` files under ``raw_dir``, deduped by resolved full path
    (never by basename).

    The old implementation combined ``(raw_dir / "metadata").rglob(...)``
    and ``raw_dir.rglob(...)`` into one list, then deduped by *name* -- a
    step that was doing double duty as both a same-file dedup and an
    unintentional "collapse distinct files that happen to share a name"
    step. That second behaviour was the bug this migration fixes (Task 9):
    two real, distinct metadata files named ``PMC1.json`` in different
    subdirectories (e.g. two download batches) would silently collapse to
    one discovered file, and downstream, to one checkpoint marker -- so the
    second file's success would be misreported as an already-done skip and
    its row would never be written.

    The explicit ``meta_dir``-specific branch is also redundant once dedup
    is path-based: ``meta_dir`` (``raw_dir / "metadata"``) is a subdirectory
    of ``raw_dir``, and PMC's real download layout
    (``download_pmc.py::process_one`` writes metadata only to
    ``output_dir / "metadata" / f"{version_id}.json"``, never elsewhere
    under ``raw_dir``) never puts a *different* JSON file at the same
    relative path outside ``metadata/``. ``discover_input_files(raw_dir,
    ["PMC*.json"])`` already walks ``raw_dir`` recursively via
    ``root.rglob(pat)``, so it finds every file the old ``meta_dir.rglob``
    branch found -- literally the same files, same resolved paths -- making
    that branch pure duplication, not a source of files the single-root
    walk would otherwise miss under the normal (non-symlinked) layout
    ``download_pmc.py`` actually creates via plain ``mkdir(parents=True)``.
    One narrow exception: ``Path.rglob`` does not descend into a directory
    *symlink* by default (``recurse_symlinks=False``), so if some external
    ops setup ever replaced ``raw_dir / "metadata"`` with a symlink to a
    separate volume, a single ``raw_dir.rglob(...)`` walk would miss files
    under it where the old dedicated ``meta_dir.rglob(...)`` branch (rooted
    directly at the symlink) would not. Not a real risk for this codebase's
    own downloader, so not special-cased here, but flagged for whoever next
    touches this function. (The old ``sync_meta.txt`` exclusion is also
    dropped here: that file is a plain ``.txt`` summary written by
    ``download_pmc.py::main``, which the ``PMC*.json`` glob pattern was
    never going to match in the first place.)
    """
    files = discover_input_files(raw_dir, ["PMC*.json"])
    files.sort(key=lambda p: input_key(p, raw_dir))
    return files


def pmc_row_bytes(row: dict[str, Any]) -> int:
    """Estimated bytes of one row: the UTF-8 length of every string value and
    of every string element of a list value, plus ``PART_ROW_OVERHEAD_BYTES``."""
    total = PART_ROW_OVERHEAD_BYTES
    for value in row.values():
        if isinstance(value, str):
            total += len(value.encode("utf-8"))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    total += len(item.encode("utf-8"))
    return total


def _part_caps() -> tuple[int, int]:
    """``(part_rows, part_bytes)`` from the runtime profile (a test seam)."""
    profile = get_profile()
    return profile.part_rows, profile.part_bytes


def _staging_dir(processed_dir: Path) -> Path:
    return Path(processed_dir) / "staging" / SOURCE


def _best_effort_audit(name: str, n_rows: int) -> None:
    """One chained ``extract_commit`` audit row on a fresh connection, degrading
    to the file-only mirror when no DB is reachable. Never raises.
    ``process_bucket`` calls it for a part BEFORE that part's ``mark_success``
    (a stop between the two gives a duplicate audit row on the rerun, never a
    missing one)."""
    with _AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "extract_commit",
                    conn=_conn,
                    object=f"{SOURCE} {name}",
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
                    object=f"{SOURCE} {name}",
                    rows_affected=n_rows,
                    note="db_unavailable_or_failed",
                    run_id=get_settings().run_id,
                )
            except Exception:  # noqa: BLE001 - last-resort fallback must never escape
                _LOG.warning("audit mirror_only fallback also failed for %s", name, exc_info=True)


def _delete_all_parts(processed_dir: Path, key: str) -> None:
    """Delete every part of bucket ``key``: the success, failed and
    load-success markers first, then the shards (the order of
    ``serialize_pubchem._delete_parts_above(key, 0)``).

    A part with a load-success marker was loaded: its name goes into the
    retire pending file BEFORE any delete, so a stop at any point here loses
    no name. Markers go before shards: a stop between leaves a shard without
    a marker, never a marker without data."""
    pattern = re.compile(re.escape(key) + r"__part(\d{5,})\.")
    prefix = glob.escape(f"{key}__part") + "*"
    load_dir = load_success_marker_path(processed_dir, SOURCE, "x").parent
    marker_dirs = [
        success_marker_path(processed_dir, SOURCE, "x").parent,
        failed_marker_path(processed_dir, SOURCE, "x").parent,
        load_dir,
    ]
    loaded: list[str] = []
    if load_dir.is_dir():
        for p in load_dir.glob(prefix):
            m = pattern.match(p.name)
            if m:
                loaded.append(f"{key}__part{m.group(1)}")
    if loaded:
        # The bucket's own file: buckets run in worker processes at once.
        parts_retire.add_retire_pending(processed_dir, SOURCE, loaded, unit=key)
    for d in [*marker_dirs, _staging_dir(processed_dir)]:
        if not d.is_dir():
            continue
        for p in d.glob(prefix):
            if pattern.match(p.name):
                p.unlink(missing_ok=True)


def _read_bucket_marker(processed_dir: Path, key: str) -> dict[str, Any] | None:
    """The stats of the bucket success marker when it has the parts format
    (``parts`` an int, ``parts_list`` the names of parts 1..parts, ``inputs``
    a list), else ``None``."""
    p = success_marker_path(processed_dir, SOURCE, key)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    st = payload.get("stats") if isinstance(payload, dict) else None
    if not isinstance(st, dict):
        return None
    parts = st.get("parts")
    if not isinstance(parts, int) or isinstance(parts, bool) or parts < 0:
        return None
    if st.get("parts_list") != [f"{key}__part{n:05d}" for n in range(1, parts + 1)]:
        return None
    if not isinstance(st.get("inputs"), list):
        return None
    return st


class PmcBucketError(ValueError):
    """A permanent error in the bucket machinery; it fails the bucket."""


def process_bucket(
    manifest_path: Path,
    *,
    raw_dir: Path,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    """Extract one bucket (its manifest is ``manifest_path``) as bounded parts.

    The bucket is skipped when its marker has the parts format and its
    ``inputs`` equal the manifest fingerprint (unless ``force``). Else every
    existing part is deleted and the bucket is written again from part 1. An
    article whose parse fails permanently is left out and recorded; the
    bucket goes on.
    """
    manifest_path = Path(manifest_path)
    key = manifest_path.stem
    t0 = time.time()
    n_rows_new = 0
    current_part: int | None = None
    try:
        manifest = read_manifest(manifest_path)
        key = str(manifest["key"])
        bucket = int(manifest["bucket"])
        fingerprint = manifest["fingerprint"]
        if not force:
            done = _read_bucket_marker(processed_dir, key)
            if done is not None and done["inputs"] == fingerprint:
                return {"source_file": key, "skipped": True, "reason": "success_marker"}
            if is_success(processed_dir, SOURCE, key):
                _LOG.warning(
                    "%s: %s; writing the bucket again from part 1",
                    key,
                    "the bucket marker has no parts format"
                    if done is None
                    else "an input file changed since the bucket completed",
                )
        # The bucket marker goes first: a stop during the rebuild leaves no
        # marker with an old parts_list.
        success_marker_path(processed_dir, SOURCE, key).unlink(missing_ok=True)
        _delete_all_parts(processed_dir, key)

        cap_rows, cap_bytes = _part_caps()
        files = manifest_files(manifest, raw_dir)
        failed: list[dict[str, str]] = []
        n_failed = 0
        seen_ids: dict[str, str] = {}
        status_counts: Counter[str] = Counter()

        def _bucket_rows() -> Iterator[dict[str, Any]]:
            """One row per article, parsed when the writer asks for it."""
            nonlocal n_failed
            for meta in files:
                akey = input_key(meta, raw_dir)
                try:
                    row = row_from_meta_and_xml(meta, raw_dir, search=False)
                except Exception as e:  # noqa: BLE001
                    if is_transient(e):
                        raise  # the whole bucket retries
                    n_failed += 1
                    if len(failed) < _FAILED_ARTICLES_LISTED:
                        failed.append({"file": akey, "error": str(e)})
                    mark_failed(
                        processed_dir,
                        SOURCE,
                        akey,
                        error_class="parse_error",
                        message=str(e),
                        exc=e,
                    )
                    continue
                other = seen_ids.get(row["id"])
                if other is not None:
                    raise PmcBucketError(
                        f"bucket {key}: {other} and {akey} map to the same row id {row['id']}"
                    )
                seen_ids[row["id"]] = akey
                # A failed marker from an earlier run of this article is stale.
                failed_marker_path(processed_dir, SOURCE, akey).unlink(missing_ok=True)
                yield row

        rows_src = _bucket_rows()
        n = 0
        with contextlib.closing(rows_src):
            pending = next(rows_src, None)
            while pending is not None:
                n += 1
                current_part = n
                psf = part_name(bucket, n)
                part: dict[str, Any] = {
                    "rows": 0,
                    "est_bytes": 0,
                    "first": None,
                    "last": None,
                    "counts": Counter(),
                }

                def _part_rows(psf: str = psf, part: dict[str, Any] = part):
                    nonlocal pending
                    while pending is not None:
                        row = pending
                        row_est = pmc_row_bytes(row)
                        # A row that does not fit ends the part and stays
                        # pending for the next one; a single row over the cap
                        # goes alone in its own part.
                        if part["rows"] and part["est_bytes"] + row_est > cap_bytes:
                            return
                        pending = None
                        row["source_file"] = psf
                        part["rows"] += 1
                        part["est_bytes"] += row_est
                        if part["first"] is None:
                            part["first"] = row.get("pmcid")
                        part["last"] = row.get("pmcid")
                        part["counts"][str(row.get("extract_status"))] += 1
                        yield row
                        # The row cap check comes before the next pull, so a
                        # part never parses an article past its last row.
                        if part["rows"] >= cap_rows:
                            return
                        pending = next(rows_src, None)

                gen = _part_rows()
                try:
                    write_info = write_rows(
                        gen,
                        processed_dir,
                        source=SOURCE,
                        source_file=psf,
                        prefer_parquet=True,
                        batch_size=_WRITE_BATCH_ROWS,
                    )
                finally:
                    gen.close()
                part_stats = {
                    "n_rows": part["rows"],
                    "first_pmcid": part["first"],
                    "last_pmcid": part["last"],
                    "extract_status_counts": dict(part["counts"]),
                    "part_rows": cap_rows,
                    "part_bytes": cap_bytes,
                    "est_bytes": part["est_bytes"],
                    "write": write_info,
                }
                # Audit before the marker: a stop between the two gives a
                # duplicate audit row on the rerun, never a marked part
                # without one.
                _best_effort_audit(psf, part["rows"])
                mark_success(processed_dir, SOURCE, psf, stats=part_stats)
                n_rows_new += part["rows"]
                status_counts.update(part["counts"])
                current_part = n + 1
                if pending is None:  # else a row that did not fit waits
                    pending = next(rows_src, None)

        stats: dict[str, Any] = {
            "n_rows": n_rows_new,
            "parts": n,
            "parts_list": [part_name(bucket, i) for i in range(1, n + 1)],
            "inputs": fingerprint,
            "articles": len(files),
            "superseded_versions": int(manifest.get("superseded_versions") or 0),
            "duplicate_files": int(manifest.get("duplicate_files") or 0),
            "failed_articles": failed,
            "failed_articles_count": n_failed,
            "part_rows": cap_rows,
            "part_bytes": cap_bytes,
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": round(time.time() - t0, 4),
        }
        mark_success(processed_dir, SOURCE, key, stats=stats)
        # The fingerprint stays in the marker only: it can be large.
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
            stats={"n_rows_partial": n_rows_new, "failed_part": current_part},
            exc=e,
        )
        return {"source_file": key, "skipped": False, "ok": False, "error": str(e)}


def _unit_name(result: dict[str, Any]) -> None:
    """A worker crash names the unit by the manifest basename (``pmc_bNNNN.json``,
    see ``parallel._failed_result``): strip ``.json`` so it is the bucket key."""
    sf = str(result.get("source_file") or "")
    if sf.startswith("pmc_b") and sf.endswith(".json"):
        result["source_file"] = sf[: -len(".json")]


def _print_verbose(result: dict[str, Any]) -> None:
    """Print ``ok``/``skip``/``FAIL`` for one bucket's result to stderr."""
    key = result.get("source_file")
    if result.get("skipped"):
        print(f"skip {key}", file=sys.stderr)
    elif result.get("ok"):
        print(
            f"ok {key} parts={result.get('parts')} rows={result.get('n_rows')} "
            f"failed_articles={result.get('failed_articles_count')}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {key}: {result.get('error')}", file=sys.stderr)


def _select_buckets(
    keys: list[str], skip_units: list[str] | None, skip_reason: str | None
) -> list[str]:
    """Drop the buckets named by ``--skip-unit`` (bucket keys). A name that
    matches no bucket exits 2 before any audit row (``apply_skips``)."""
    if not skip_units:
        return keys
    from episteme.ops import skip

    keep = set(
        skip.apply_skips(
            SOURCE, "extract", keys, skip_units, skip_reason, run_id=get_settings().run_id
        )
    )
    for k in keys:
        if k not in keep:
            print(f"skip-unit {k}", file=sys.stderr)
    return [k for k in keys if k in keep]


def extract_pmc(
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
    """Importable core: extract the PMC metadata/JATS under ``raw_dir`` by
    fixed id bucket into staging parts + ops markers under ``processed_dir``.

    ``max_files`` limits the number of buckets; ``skip_units`` take bucket
    keys. Returns ``{"inputs", "ok", "failed", "rows", "buckets", "articles",
    "unrecognised_files", "superseded_versions", "duplicate_files",
    "failed_articles_count"}``; ``inputs`` and ``buckets`` both count the
    buckets of this run.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    grouped, unrecognised = group_by_bucket(discover_meta_files(raw_dir), raw_dir)
    if unrecognised:
        _LOG.warning(
            "%d metadata file name(s) under %s are not PMC<id>.<version>.json; left out",
            unrecognised,
            raw_dir,
        )
    by_key = {bucket_key(b): grouped[b] for b in sorted(grouped)}
    keys = _select_buckets(list(by_key), skip_units, skip_reason)
    if max_files and max_files > 0:
        keys = keys[:max_files]
    manifests = [write_manifest(processed_dir, by_key[k], raw_dir) for k in keys]

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

    ok = sum(1 for r in results if r.get("ok"))
    failed = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    rows = sum(int(r.get("n_rows") or 0) for r in results if r.get("ok"))
    summary = {
        "inputs": len(keys),
        "ok": ok,
        "failed": failed,
        "rows": rows,
        "buckets": len(keys),
        "articles": sum(len(by_key[k].files) for k in keys),
        "unrecognised_files": unrecognised,
        "superseded_versions": sum(by_key[k].superseded for k in keys),
        "duplicate_files": sum(by_key[k].duplicate_files for k in keys),
        "failed_articles_count": sum(int(r.get("failed_articles_count") or 0) for r in results),
    }

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


def _main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(description="PMC JATS/metadata to episteme.articles staging shards")
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "pmc" / "oa_comm")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all buckets; N = the first N id buckets (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = extract_pmc(
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
        print(f"ERROR: no PMC*.json under {raw_dir}", file=sys.stderr)
        return 1

    print(f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']}")
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
