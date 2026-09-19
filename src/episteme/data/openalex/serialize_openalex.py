#!/usr/bin/env python3
"""Structured serializer: OpenAlex -- parse OpenAlex's real ``works_jsonl``
snapshot format (one JSON object per line, optionally gzip-compressed) into
the unified ``episteme.articles`` row schema (contract v1.4), filtered to a
BOUNDED BIOMEDICAL SUBSET (SP4 spec Sec 8 open item 1 -- resolved by this
task, see "Bounded biomedical filter" section below).

This is the LAST of SP4's 8 structured serializers (task 12 of 13) and the
ONLY SP4 source with a genuinely literature-like bibliographic shape: real
``title``/``authorships``/``publication_year``/``primary_location`` (journal)/
``doi``, plus an ``abstract_inverted_index`` field that must be reconstructed
into plain text (OpenAlex never ships a plain-text abstract, for copyright
reasons -- see "Abstract reconstruction" below).

Real-data confirmation (2026-09-17, NOT guessed): the wrapper
``download_openalex.sh`` explicitly IGNORES ``--max-files`` for openalex (an
unbounded ``aws s3 sync``/``s5cmd sync`` of the entire, hundreds-of-GB
``s3://openalex/data/jsonl/works`` prefix -- confirmed by reading the wrapper
and ``scripts/data/_lib/common.sh``'s ``s3_sync`` directly, NOT run in full
this task, see task-12-report.md). This module's real-data proofs (the
fixture AND the real end-to-end run) instead used a genuinely-sliced REAL
shard: a scoped, public (``--no-sign-request``, no AWS credentials needed)
byte-range fetch of the first ~3MB of one real object,
``s3://openalex/data/jsonl/works/updated_date=2026-06-26/part_0000.gz``,
decompressed with ``gunzip`` (which emits every complete leading gzip block
before erroring "unexpected end of file" on the deliberately-truncated
tail -- 1990 real, complete, individually ``json.loads``-valid work records
recovered from a single partial fetch, the truncated final line dropped).
Real OpenAlex S3 object keys land as bare ``part_NNNN.gz`` (no ``.jsonl`` in
the name) under ``updated_date=YYYY-MM-DD/`` partition subdirectories --
confirmed directly against the real bucket listing, not assumed.

Unit of work: one ``works_jsonl`` shard file (brief's own "same shape as
Task 5" framing) -- one row per ACCEPTED (biomedical, see below) work record
in that file, one success/failure marker per shard.

--- Bounded biomedical filter (SP4 spec Sec 8, open item 1 -- RESOLVED here) ---

Chosen filter: a work is IN-SCOPE iff its real ``concepts`` array (OpenAlex's
controlled, hierarchical concept taxonomy -- every tagged concept carries a
``level`` 0-5, where level 0 is one of 19 fixed top-level domains) contains
at least one entry whose ``level == 0`` AND whose bare concept id (the
``/C\\d+`` suffix of the real ``https://openalex.org/C...`` URL id) is one of:

    Medicine  -> C71924100
    Biology   -> C86803240

These two real, confirmed IDs (read directly off a real fetched shard's own
level-0 concept entries at implementation time, not looked up from external
docs) are this task's concrete resolution of the brief's "concept/topic tag
match against a biomedical allow-list ... filter to concepts under the
'Medicine'/'Biology' top-level branches" recommendation (Step 4). ``topics``/
``primary_topic`` (OpenAlex's newer, narrower taxonomy) was considered and
REJECTED as the primary signal: confirmed empirically against the real 1990-
record sample that ``topics`` is populated on only 1361/1990 (68%) of real
records -- ``concepts`` is populated far more consistently (every record in
the sample carried a full concept list, including a level-0 entry) and is
therefore the safer, higher-coverage field for a filter whose false-negative
rate matters (a biomedical work with no ``topics`` entry must still be
caught). A record's concept list carries its FULL ancestry (a level-2/3/4
specific concept's level-0 ancestor concept co-occurs in the same flat list
-- confirmed empirically, not assumed), so this flat membership test needs no
separate concept-tree lookup or external allow-list file.

A rejected (non-biomedical) record is NOT an error: it is parsed, counted
(``n_records`` / ``n_rejected_non_biomedical`` in this module's own filter
stats, mirrored into ``--report``'s output and the per-file ``mark_success``
stats payload), and simply excluded from ``rows`` -- same disposition as
SP2's ``extract_guidelines.py`` QA-shaped-split skip (counted as an input,
never surfaced as a failure). See ``is_biomedical()``, a small pure function,
independently unit-tested.

Real rejection-rate finding (task-12-report.md carries the exact numbers):
confirmed NOT 0% and NOT 100% against the real 1990-record sample (308
Medicine + 229 Biology level-0 hits, of 1990 total, before de-duplicating
records that hit both branches) -- a genuinely bounded, non-trivial subset,
not a no-op filter and not an empty one.

--- Abstract reconstruction ---

OpenAlex never ships a plain-text abstract (a documented copyright-avoidance
measure): ``abstract_inverted_index`` is ``{word: [position, position, ...]}``
-- a word can recur at multiple positions (confirmed on real data: "and" at
12 distinct positions in this module's own fixture's first record). Correct
reconstruction is a small, PURE, independently-testable function
(``reconstruct_abstract``): flatten to ``(position, word)`` pairs across
every word (not just the first occurrence), sort by position, join with
spaces. A naive ``sorted(dict.items())`` would sort ALPHABETICALLY BY WORD
(wrong order, a real bug class this function deliberately avoids) -- see
tests/data/test_serialize_openalex.py's dedicated unit tests, including the
brief's own worked example and a repeated-word-at-multiple-positions case.

--- Bibliographic field mapping (the one SP4 source with real ones) ---

``id`` = ``f"openalex:{native}"`` where ``native`` strips any
``https://openalex.org/`` URL prefix off the real ``id`` field (e.g.
``https://openalex.org/W7165474278`` -> ``W7165474278`` -- confirmed the real
field DOES carry this full-URL shape, not a bare id, against the real
fetched shard). ``doi`` strips any ``https://doi.org/`` prefix the same way
(real field: ``https://doi.org/10.xxxx/...``) to a bare DOI string, matching
``extract_pubmed.py``'s bare-DOI convention. ``title`` = the real ``title``
field, falling back to ``display_name`` (same string in every real record
sampled, but ``title`` is occasionally null while ``display_name`` is not).
``year`` = ``publication_year``. ``authors`` = ``[a.author.display_name for
a in authorships]`` (real field; ``author.id`` is frequently ``null`` for
poorly-disambiguated authors -- ``display_name`` is always present when an
authorship entry exists). ``journal`` = ``primary_location.source
.display_name`` (real field; can be a journal, repository, or other source
type -- OpenAlex indexes datasets/preprints/etc. under the same ``works``
schema, not just journal articles). ``mesh`` = deduplicated
``descriptor_name`` values off the real (often empty) ``mesh`` array, a
bonus enrichment beyond the brief's own minimum ask. ``text`` is left unset
here and built by ``article_schema.finalize_row``'s ``build_text(title,
abstract)`` -- this is a literature-shaped source (title+abstract IS the
right ``text``), unlike every other SP4 structured source's synthetic
sentence template.

--- Licence ---

OpenAlex Help Center, "Access > Overview" page (fetched directly via a real
browse session at implementation time, 2026-09-17,
https://help.openalex.org/access/overview/, page footer states "Last updated
15 August 2026"): "However you get it, the data itself is free and open
under CC0 -- what costs money at the higher tiers is the service of serving
and refreshing it." This is the OpenAlex DATASET's own metadata licence
(applies uniformly to every row this module emits), run through the ordinary
``article_schema.normalize_license()`` machinery (NOT a hardcode) --
confirmed it hits the existing CC0 arm (the literal ``CC0`` token), exactly
as the brief predicted. Resolves to ``license="CC0"`` ->
``subset_from_license("CC0") == "commercial"``.

Deliberately NOT used: each real work's own ``primary_location.license``
field (a PER-ARTICLE OA licence of the underlying publication, e.g.
``"cc-by"``, frequently ``null`` -- confirmed varying across this module's
own fixture rows). Mirrors reactome/pubchem/clinvar's single-fixed-licence-
per-source convention: this module documents ONE dataset-level licence
string, not a per-row lookup.

Importable core: ``serialize_openalex(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table
(including the biomedical-filter rejection breakdown) without writing any
shard, marker, manifest or audit row.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5):
  python -m episteme.data.openalex.serialize_openalex \\
    --raw-dir ./01_raw/openalex \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    [--force]
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

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
    write_run_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "openalex"

# OpenAlex Help Center, "Access > Overview" (help.openalex.org/access/overview/,
# fetched directly at implementation time, 2026-09-17) -- contains the literal
# "CC0" token. See module docstring's "Licence" section for the full quote and
# the deliberate choice NOT to use each work's own per-article
# primary_location.license field.
_OPENALEX_LICENSE_RAW = (
    "OpenAlex Help Center, 'Access > Overview' (help.openalex.org/access/overview/): "
    '"However you get it, the data itself is free and open under CC0 -- what '
    'costs money at the higher tiers is the service of serving and refreshing it." '
    "This is the OpenAlex dataset's own metadata licence, not any individual "
    "work's per-article OA licence (a separate, per-record field)."
)

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors extract_pubmed.py / every prior SP4 structured serializer.
_AUDIT_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Bounded biomedical filter (SP4 spec Sec 8 open item 1 -- resolved here).
# See module docstring's "Bounded biomedical filter" section for the full
# reasoning (concepts vs topics coverage, real confirmed IDs, disposition).
# --------------------------------------------------------------------------- #

# Real, confirmed OpenAlex level-0 (top-level domain) concept ids for the two
# biomedical branches -- read directly off a real fetched shard's own level-0
# concept entries (s3://openalex/data/jsonl/works/updated_date=2026-06-26/
# part_0000.gz, partial fetch, see module docstring), NOT looked up from
# external docs.
_BIOMEDICAL_CONCEPT_IDS = frozenset({"C71924100", "C86803240"})  # Medicine, Biology


def _bare_id(raw: str | None) -> str | None:
    """``https://openalex.org/C71924100`` -> ``C71924100`` (also passes a
    bare id, or any other trailing-path-segment id, through unchanged --
    shared by concept/work id stripping)."""
    if not raw:
        return None
    s = str(raw).strip()
    return s.rsplit("/", 1)[-1] if s else None


def is_biomedical(rec: dict[str, Any]) -> bool:
    """The bounded biomedical filter (spec Sec 8 open item 1): True iff at
    least one of this work's real ``concepts`` entries is a LEVEL-0
    (top-level-domain) concept whose bare id is Medicine or Biology (see
    ``_BIOMEDICAL_CONCEPT_IDS``). Pure, independently unit-tested. A False
    result is NOT an error -- see module docstring's disposition note."""
    for c in rec.get("concepts") or []:
        if c.get("level") == 0 and _bare_id(c.get("id")) in _BIOMEDICAL_CONCEPT_IDS:
            return True
    return False


# --------------------------------------------------------------------------- #
# Abstract reconstruction -- small, pure, independently-testable.
# --------------------------------------------------------------------------- #


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """OpenAlex ``abstract_inverted_index`` (``{word: [position, ...]}``, a
    word may recur at multiple positions) -> plain-text abstract, or
    ``None`` if absent/empty.

    Flattens to ``(position, word)`` pairs across ALL occurrences of every
    word (not just the first), sorts by position, joins with spaces. Pure
    function, no I/O -- see tests/data/test_serialize_openalex.py for the
    brief's own worked example plus a repeated-word-at-multiple-positions
    case proving this does NOT degrade to a naive (and wrong)
    ``sorted(dict.items())`` alphabetical-by-word ordering.
    """
    if not inverted_index:
        return None
    pairs: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            pairs.append((pos, word))
    pairs.sort(key=lambda p: p[0])
    text = " ".join(word for _, word in pairs)
    return text or None


def _bare_doi(raw: str | None) -> str | None:
    """``https://doi.org/10.xxxx/...`` -> ``10.xxxx/...`` (bare DOI, matches
    extract_pubmed.py's convention); a bare DOI or ``None`` passes through
    unchanged."""
    if not raw:
        return None
    s = str(raw).strip()
    m = re.match(r"^https?://(dx\.)?doi\.org/(.+)$", s, re.I)
    return m.group(2) if m else s


def openalex_row(rec: dict[str, Any], source_file: str) -> dict[str, Any]:
    """One real OpenAlex ``works_jsonl`` record (already confirmed
    biomedical by the caller -- see ``is_biomedical``) -> a finalized
    ``episteme.articles`` row. See module docstring's "Bibliographic field
    mapping" section for the full field-by-field rationale."""
    native_id = _bare_id(rec.get("id"))
    doi = _bare_doi(rec.get("doi"))
    title = rec.get("title") or rec.get("display_name")
    year_raw = rec.get("publication_year")
    try:
        year = int(year_raw) if year_raw is not None else None
    except (TypeError, ValueError):
        year = None
    abstract = reconstruct_abstract(rec.get("abstract_inverted_index"))

    authors = [
        a.get("author", {}).get("display_name")
        for a in (rec.get("authorships") or [])
        if a.get("author", {}).get("display_name")
    ] or None

    journal = None
    source = (rec.get("primary_location") or {}).get("source") or {}
    if source.get("display_name"):
        journal = source["display_name"]

    mesh_list: list[str] = []
    seen_mesh: set[str] = set()
    for m in rec.get("mesh") or []:
        name = m.get("descriptor_name") if isinstance(m, dict) else None
        if name and name not in seen_mesh:
            seen_mesh.add(name)
            mesh_list.append(name)
    mesh = mesh_list or None

    lic, lic_url, lic_raw = normalize_license(_OPENALEX_LICENSE_RAW)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"{SOURCE}:{native_id}" if native_id else f"{SOURCE}:{source_file}:unknown",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": native_id,
        "pmid": None,
        "pmcid": None,
        "doi": doi,
        "title": title or None,
        "abstract": abstract,
        "body_text": None,
        "authors": authors,
        "journal": journal,
        "year": year,
        "mesh": mesh,
        "publication_types": None,
        "language": rec.get("language") or None,
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_raw,
        "subset": subset,
        "is_retracted": (
            bool(rec.get("is_retracted")) if rec.get("is_retracted") is not None else False
        ),
        "pmc_version": None,
        "is_manuscript": None,
        "is_historical_ocr": None,
        "pdf_url": None,
        "container_id": None,
        "book_meta": None,
    }
    return finalize_row(row)


def _open_maybe_gzip(path: Path):
    """Real OpenAlex S3 object keys land as bare ``part_NNNN.gz`` (gzip-
    compressed JSONL, no ``.jsonl`` in the name -- confirmed against a real
    fetched object, see module docstring). A manually-supplied fixture/test
    file may be a plain, uncompressed ``.jsonl`` -- both are supported,
    detected by suffix."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def iter_rows_from_file(
    path: Path,
    stats: Counter[str] | None = None,
    *,
    source_file: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream one ``episteme.articles`` row per ACCEPTED (biomedical) work
    record in one real OpenAlex ``works_jsonl`` shard file (plain ``.jsonl``
    or gzip-compressed ``.gz``).

    A non-biomedical record is NOT an error: parsed, counted in ``stats``,
    excluded from the yield -- see module docstring's "Bounded biomedical
    filter" disposition note. A line that fails to parse as JSON is also
    counted and skipped, never raises -- one malformed line must not fail
    the whole shard (a real, large multi-GB shard could plausibly carry a
    handful of truncated/corrupt lines at a partition boundary).

    ``stats`` (a ``collections.Counter``, mutated in place if supplied) ends
    up carrying ``n_records`` (total parsed), ``n_accepted`` (biomedical,
    yielded), ``n_rejected_non_biomedical``, and ``n_parse_errors`` -- the
    record-level "inputs vs rows" split the brief's Step 6 sign-off reports
    (a finer grain than the file-level ``inputs``/``rows`` counters
    ``serialize_openalex``'s own summary dict returns).
    """
    if stats is None:
        stats = Counter()
    source_file = source_file or path.name
    with _open_maybe_gzip(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats["n_parse_errors"] += 1
                continue
            stats["n_records"] += 1
            if not is_biomedical(rec):
                stats["n_rejected_non_biomedical"] += 1
                continue
            stats["n_accepted"] += 1
            yield openalex_row(rec, source_file)


def discover_openalex_files(raw_dir: Path) -> list[Path]:
    """Discover real OpenAlex works_jsonl shard files under ``raw_dir``.

    ``download_openalex.sh``'s real ``works_jsonl`` mode syncs OpenAlex's own
    S3 object keys VERBATIM (bare ``part_NNNN.gz``), landing recursively under
    ``updated_date=YYYY-MM-DD/`` partition subdirectories, so every partition
    holds a same-named ``part_0000.gz``. ``*.jsonl``/``*.jsonl.gz`` are also
    matched for manually-supplied fixture/test raw dirs.

    Uses the shared :func:`checkpoint_markers.discover_input_files` (dedup by
    resolved FULL path, never basename) and sorts by
    :func:`checkpoint_markers.input_key`, the raw-dir-relative identity that
    also keys the ops markers, staging shards, audit rows and each row's
    ``source_file``."""
    raw_dir = Path(raw_dir)
    files = discover_input_files(raw_dir, ["part_*.gz", "*.jsonl", "*.jsonl.gz"])
    files.sort(key=lambda p: input_key(p, raw_dir))
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror every prior SP4 structured serializer's ``_best_effort_audit``
    shape verbatim, including the settled ``event_type="serialize_commit"``
    ruling (NOT ``"extract_commit"`` -- settled by Tasks 4/5/6, not
    re-litigated here). One chained audit row on a fresh connection,
    degrading to the file-only mirror when no DB is reachable. Never raises
    -- ``mark_success`` has already run for this file."""
    with _AUDIT_LOCK:
        try:
            from episteme.data.db.connection import connection as _pg_connection

            with _pg_connection() as _conn:
                _audit(
                    "serialize_commit",
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
                    "serialize_commit",
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
    raw_dir: Path,
    force: bool,
) -> dict[str, Any]:
    # Raw-dir-relative identity (e.g. updated_date=.../part_0000.gz), never a
    # bare basename: every partition holds a same-named part_0000.gz.
    basename = input_key(path, raw_dir)
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    filter_stats: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    try:
        for row in iter_rows_from_file(path, filter_stats, source_file=basename):
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
            "n_records_seen": filter_stats.get("n_records", 0),
            "n_accepted_biomedical": filter_stats.get("n_accepted", 0),
            "n_rejected_non_biomedical": filter_stats.get("n_rejected_non_biomedical", 0),
            "n_parse_errors": filter_stats.get("n_parse_errors", 0),
            "elapsed_sec": elapsed,
            "write": write_info,
        }
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        _best_effort_audit(basename, len(rows))
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class=type(e).__name__,
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
            f"biomed={result.get('n_accepted_biomedical')}/{result.get('n_records_seen')} "
            f"status={result.get('extract_status_counts')}",
            file=sys.stderr,
        )
    else:
        print(f"FAIL {basename}: {result.get('error')}", file=sys.stderr)


def serialize_openalex(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse real OpenAlex ``works_jsonl`` shard files under
    ``raw_dir`` into staging shards + ops markers under ``processed_dir``,
    filtered to the bounded biomedical subset (see module docstring).

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n,
    "n_records_seen": n, "n_accepted_biomedical": n,
    "n_rejected_non_biomedical": n}`` -- ``inputs``/``ok``/``failed``/``rows``
    are FILE-level counters (matching every other SP4 structured
    serializer's summary shape); the ``n_records_seen`` / ``n_accepted_*`` /
    ``n_rejected_*`` counters are the finer-grained RECORD-level biomedical-
    filter split the brief's Step 6 sign-off reports.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_openalex_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    results: list[dict[str, Any]] = []
    t0 = time.time()

    if workers == 1:
        for fp in files:
            result = process_one(fp, processed_dir=processed_dir, raw_dir=raw_dir, force=force)
            results.append(result)
            if verbose:
                _print_verbose(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(
                    process_one, fp, processed_dir=processed_dir, raw_dir=raw_dir, force=force
                ): fp
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
    n_records_seen = sum(int(r.get("n_records_seen") or 0) for r in results if r.get("ok"))
    n_accepted = sum(int(r.get("n_accepted_biomedical") or 0) for r in results if r.get("ok"))
    n_rejected = sum(int(r.get("n_rejected_non_biomedical") or 0) for r in results if r.get("ok"))
    summary = {
        "inputs": len(files),
        "ok": ok,
        "failed": failed,
        "rows": rows,
        "n_records_seen": n_records_seen,
        "n_accepted_biomedical": n_accepted,
        "n_rejected_non_biomedical": n_rejected,
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
    except Exception:  # noqa: BLE001 -- manifest is best-effort, never fail the run on it
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


def _render_field_shape(
    rows: list[dict[str, Any]], n_files: int, filter_stats: Counter[str]
) -> str:
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

    n_records = filter_stats.get("n_records", 0)
    n_accepted = filter_stats.get("n_accepted", 0)
    n_rejected = filter_stats.get("n_rejected_non_biomedical", 0)
    n_parse_errors = filter_stats.get("n_parse_errors", 0)
    pct_rejected = round(100.0 * n_rejected / n_records, 1) if n_records else 0.0
    lines += ["", "bounded biomedical filter (spec Sec 8 item 1):"]
    lines.append(
        f"  records_seen={n_records}  accepted={n_accepted}  "
        f"rejected_non_biomedical={n_rejected} ({pct_rejected}%)  "
        f"parse_errors={n_parse_errors}"
    )

    return "\n".join(lines) + "\n"


def _run_report(raw_dir: Path, max_files: int = 0) -> int:
    files = discover_openalex_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no openalex works_jsonl files under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    filter_stats: Counter[str] = Counter()
    for fp in files:
        rows.extend(iter_rows_from_file(fp, filter_stats, source_file=input_key(fp, raw_dir)))
    print(_render_field_shape(rows, len(files), filter_stats), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        prog="python -m episteme.data.openalex.serialize_openalex",
        description="OpenAlex works_jsonl (bounded biomedical subset) to "
        "episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "openalex")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered works_jsonl shard files (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_openalex(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no openalex works_jsonl files under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']} "
        f"records_seen={res['n_records_seen']} "
        f"accepted={res['n_accepted_biomedical']} "
        f"rejected_non_biomedical={res['n_rejected_non_biomedical']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
