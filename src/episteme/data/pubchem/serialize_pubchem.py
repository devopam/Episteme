#!/usr/bin/env python3
"""Structured serializer: PubChem -- parse a downloaded PubChem
``compound_extras`` property TSV into the unified ``episteme.articles`` row
schema (contract v1.4).

Unit-of-work decision (SP4 Task 7 -- documented here per the controller's
explicit instruction, see task-7-report.md for the full writeup):
``download_pubchem.sh compound_extras`` (mode default) fetches everything
under PubChem's real ``Compound/Extras`` FTP directory -- confirmed by
running it for real (``scripts/data/run_pipeline.sh pubchem download
--max-files 3``) and by fetching ``Compound/Extras/README-Extras`` directly.
That directory does NOT contain one single joined CID/SMILES/IUPAC/formula
TSV. It contains ~15 SEPARATE per-property files, each headerless and
CID-keyed but covering only one property, e.g.:
  CID-SMILES.gz   -- CID, tab, canonical SMILES
  CID-IUPAC.gz    -- CID, tab, computed IUPAC name
  CID-Title.gz    -- CID, tab, compound-summary-page title
  CID-Mass.gz     -- CID, tab, formula, tab, monoisotopic mass, tab, exact mass
  CID-Synonym-filtered.gz, CID-MeSH, CID-PMID.gz, CID-Patent.gz, ... (unused
  here; see "Scope" below)

This module takes DESIGN OPTION 1 of the two the controller flagged as
defensible: ``CID-SMILES.gz`` (or its bare/``.tsv`` fixture form) is the
PRIMARY unit of work -- one row per CID present in that file, one
success/failure marker per primary file (matching Sec 4.11's "unit of work =
one input file"). ``CID-IUPAC.gz`` / ``CID-Title.gz`` / ``CID-Mass.gz``, if
present as SIBLING files in the same directory as the primary file, are
joined in via DuckDB LEFT JOINs (best-effort, NOT required -- a primary file
with no siblings present still serializes successfully, just with sparser
text). Sibling files are NOT independent units of work: they carry no
marker of their own and are silently skipped if absent.

Reasoning for Option 1 over Option 2 ("each per-property file is its own
independent unit of work, emitting sparser single-field rows, relying on
postgres_loader's per-source_file replace + id-collision handling to merge
across files"):
  1. The brief's own illustrative template -- "Compound CID {cid}
     ({iupac_name}) has molecular formula {formula} and canonical SMILES
     {smiles})" -- needs THREE joined properties in one row. Option 2 can
     only ever populate one property per row per run; matching the brief's
     template at all requires the join.
  2. A single-property Option-2 row is short: a SMILES-only sentence for a
     small molecule (e.g. CID 4, "CC(CN)O") is ~105 chars, well under
     MIN_OK_TEXT_LEN (200) -- see the field-shape/length findings in
     task-7-report.md. Option 1's joined rows still aren't reliably over
     200 chars either (a real, well-populated small-molecule record can
     still land at ~187 chars, see the report), but at least trend longer
     and richer than any single-property row could.
  3. Option 2 relies on cross-run id-collision "merge" semantics that
     postgres_loader was never designed to guarantee field-level merging
     for (it does per-source_file DELETE+re-COPY, not per-column upsert) --
     running CID-IUPAC.gz after CID-SMILES.gz for the same id would REPLACE
     the row, not enrich it, silently discarding the SMILES. Option 1
     avoids this entirely: enrichment happens once, at parse time, within
     a single process_one() call.
Chosen primary file: ``CID-SMILES.gz``. Every live CID has a canonical
SMILES (it is the structural definition of the compound); IUPAC names and
even PubChem's own generated ``CID-Title.gz`` names are missing for some
CIDs in the real data (confirmed: comparing sampled real CID sets,
``CID-SMILES.gz`` has ~264K rows in the sampled byte range vs. ~189-232K for
the sibling files -- these ARE incomplete relative to SMILES coverage, not
guessed). SMILES is therefore the most complete anchor to drive iteration.

Scope ruling: ``CID-Synonym-filtered.gz`` / ``CID-Synonym-unfiltered.gz`` are
NOT joined -- they carry MULTIPLE rows per CID (one row per synonym), which
does not fit a simple 1:1 LEFT JOIN without an aggregation step this task's
brief does not ask for. Flagged as a possible follow-up enrichment, not
attempted here (same discipline as uniprot task-6's ``.dat``-format scope
cut).

PubChem compound-extras records carry no bibliographic shape: ``title`` /
``journal`` / ``year`` / ``authors`` are always ``None`` on the row (PubChem's
own per-CID "title" -- e.g. "Acetyl-DL-carnitine" -- is folded into ``text``
only, matching chembl/uniprot's convention of not conflating a compound/
protein display name with the bibliographic ``title`` column). ``container_id``
/ ``book_meta`` are ``None`` on every row (no book-shaped source in SP4).

Licence: PubChem's real ``Compound/Extras/README-Extras`` "Fair Use
Disclaimer" (confirmed by fetching it directly at implementation time) is
NOT a CC-variant string -- it is NCBI's own public-domain-flavoured
boilerplate ("NCBI itself places no restrictions on the use or distribution
of the data contained therein. However, some submitters ... may claim
patent, copyright, or other intellectual property rights ..."). Run through
the ordinary ``article_schema.normalize_license`` machinery (NOT a hardcode,
NOT a new arm added to normalize_license) -- confirmed it falls through
every existing arm (no CC/BY/NC/SA/ND token, no "text mining"/"fair use"
substring match despite the word "restrictions" appearing -- the raw text
does not contain the literal "FAIR USE" or "TEXT MINING" tokens the existing
arm checks for) to ``license="unknown"`` -> ``subset_from_license("unknown")``
== ``"open_metadata"`` (the conservative default). This is EXPECTED and
ACCEPTABLE per the task-7 brief (spec Sec 8 open item 2). RESOLVED in SP4.1
Task 11: ``pubchem_row`` applies a source-anchored governance override (user
decision 2026-09-19) -> ``license="public_domain"`` -> ``subset="commercial"``;
``normalize_license`` is unchanged and ``license_raw`` keeps the real text.

Importable core: ``serialize_pubchem(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5):
  python -m episteme.data.pubchem.serialize_pubchem \\
    --raw-dir ./01_raw/pubchem \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import duckdb

_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from episteme.audit_trail import record as _audit  # noqa: E402
from episteme.config import get_settings  # noqa: E402
from episteme.data.article_schema import (  # noqa: E402
    ARTICLE_COLUMNS,
    LICENSE_PUBLIC_DOMAIN,
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

_LOG = logging.getLogger(__name__)

SOURCE = "pubchem"

# PubChem's real Compound/Extras/README-Extras "Fair Use Disclaimer" section,
# fetched directly at implementation time (2026-09-16) from
# https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras/README-Extras -- NOT a
# CC-variant string. Run through normalize_license()/subset_from_license()
# like every other field -- NOT a hardcode. See module docstring's "Licence"
# section: normalize_license() yields unknown, but pubchem_row overrides it to
# public_domain -> commercial (SP4.1 Task 11, user decision 2026-09-19).
_PUBCHEM_LICENSE_RAW = (
    "Databases of molecular data on the NCBI FTP site include such examples "
    "as nucleotide sequences (GenBank), protein sequences, macromolecular "
    "structures, molecular variation, gene expression, and mapping data. "
    "They are designed to provide and encourage access within the "
    "scientific community to sources of current and comprehensive "
    "information. Therefore, NCBI itself places no restrictions on the use "
    "or distribution of the data contained therein. However, some "
    "submitters of the original data may claim patent, copyright, or other "
    "intellectual property rights in all or a portion of the data they "
    "have submitted. NCBI is not in a position to assess the validity of "
    "such claims and, therefore, cannot provide comment or unrestricted "
    "permission concerning the use, copying, or distribution of the "
    "information contained in the molecular databases."
)

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors extract_pubmed.py / serialize_chembl.py / serialize_uniprot.py.
_AUDIT_LOCK = threading.Lock()

# The PRIMARY file: one row per CID present here drives the whole run (see
# module docstring's unit-of-work decision). Candidate extensions cover the
# real download shape (bare ``.gz``, no ``.tsv`` in the name) and the
# unit-test fixture / a manually-supplied ``.tsv`` or ``.tsv.gz`` shape.
_PRIMARY_STEM = "CID-SMILES"
_PRIMARY_COLUMNS = {"cid": "BIGINT", "smiles": "VARCHAR"}
_CANDIDATE_EXTS = (".gz", ".tsv.gz", ".tsv")

# SIBLING files: best-effort enrichment, looked up by exact stem in the same
# directory as the primary file. Column shapes verified against a real
# download of each file (README-Extras + inspecting real Compound/Extras
# content directly), NOT guessed:
#   CID-IUPAC.gz  -- CID, tab, IUPAC name
#   CID-Title.gz  -- CID, tab, compound-summary-page title
#   CID-Mass.gz   -- CID, tab, formula, tab, monoisotopic mass, tab, exact mass
# Dict order also fixes SQL param order in _build_query -- do not reorder
# without checking that function.
_SIBLING_DEFS: dict[str, dict[str, Any]] = {
    "iupac_name": {
        "stem": "CID-IUPAC",
        "alias": "i",
        "columns": {"cid": "BIGINT", "iupac_name": "VARCHAR"},
    },
    "title": {
        "stem": "CID-Title",
        "alias": "t",
        "columns": {"cid": "BIGINT", "title": "VARCHAR"},
    },
    "formula": {
        "stem": "CID-Mass",
        "alias": "m",
        "columns": {
            "cid": "BIGINT",
            "formula": "VARCHAR",
            "monoisotopic_mass": "DOUBLE",
            "exact_mass": "DOUBLE",
        },
    },
}


def _columns_literal(columns: dict[str, str]) -> str:
    """``{'cid': 'BIGINT', 'smiles': 'VARCHAR'}`` -- DuckDB struct-literal
    syntax for ``read_csv(..., columns=...)``. Built from a plain dict so
    the schema is declared once, in Python, not duplicated as raw SQL text."""
    return "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"


def _find_siblings(primary_path: Path) -> dict[str, Path]:
    """Best-effort: for each sibling property, return the first existing
    candidate file in the primary file's directory, or omit the key
    entirely if none of its candidate extensions exist. Never raises --
    a primary file with zero siblings present is a normal, supported case
    (see module docstring point 2: a real ``--max-files``-capped download
    commonly fetches an alphabetically-early slice that may not include
    every sibling)."""
    directory = primary_path.parent
    found: dict[str, Path] = {}
    for key, spec in _SIBLING_DEFS.items():
        for ext in _CANDIDATE_EXTS:
            candidate = directory / f"{spec['stem']}{ext}"
            if candidate.is_file():
                found[key] = candidate
                break
    return found


def _build_query(primary_path: Path, sibling_paths: dict[str, Path]) -> tuple[str, list[str]]:
    """Build the DuckDB SQL (with ``?`` positional params) that LEFT JOINs
    whichever sibling files were actually found onto the primary CID-SMILES
    read. A missing sibling contributes a literal ``NULL`` column instead of
    a join -- the row shape (``cid``, ``smiles``, ``iupac_name``, ``title``,
    ``formula``) is always the same five columns regardless of which
    siblings are present."""
    primary_literal = _columns_literal(_PRIMARY_COLUMNS)
    select_cols = ["p.cid AS cid", "p.smiles AS smiles"]
    join_parts: list[str] = []
    params: list[str] = [str(primary_path)]

    for key, spec in _SIBLING_DEFS.items():
        path = sibling_paths.get(key)
        if path is None:
            select_cols.append(f"CAST(NULL AS VARCHAR) AS {key}")
            continue
        alias = spec["alias"]
        cols_literal = _columns_literal(spec["columns"])
        join_parts.append(
            f"LEFT JOIN read_csv(?, delim='\\t', header=false, "
            f"columns={cols_literal}) {alias} ON {alias}.cid = p.cid"
        )
        params.append(str(path))
        select_cols.append(f"{alias}.{key} AS {key}")

    sql = (
        "SELECT " + ", ".join(select_cols) + " "
        f"FROM read_csv(?, delim='\\t', header=false, columns={primary_literal}) p "
        + " ".join(join_parts)
        + " ORDER BY p.cid"
    )
    return sql, params


def _build_text(rec: dict[str, Any]) -> str:
    """Template-based declarative sentence for one CID (SP4 task-7 brief's
    illustrative example, extended with the joined title when present). No
    LLM rewriting -- every clause is a plain field substitution or omitted
    when the source/sibling field is absent.

    MIN_OK_TEXT_LEN (200 chars) sanity-check (see task-7-report.md for the
    full table): a well-populated record with LONG identifiers (e.g. CID 1,
    "Acetyl-DL-carnitine") clears 200 chars comfortably (~240). A
    well-populated record with SHORT identifiers (e.g. CID 4, a small
    3-carbon amine with a short IUPAC name and short SMILES) lands at ~187
    chars -- under the threshold despite ALL FOUR fields (SMILES, IUPAC,
    title, formula) being present and joined. This mirrors chembl's task-5
    finding almost exactly: a compact, fully-populated structured record can
    still legitimately land "partial", not "ok" -- honest, not a bug. A
    primary-only row with no siblings at all (e.g. a real download that only
    fetched CID-SMILES.gz, no enrichment files) is shorter still and will
    very often be "partial".
    """
    cid = rec.get("cid")
    smiles = rec.get("smiles")
    iupac_name = rec.get("iupac_name")
    title = rec.get("title")
    formula = rec.get("formula")

    subject = f"PubChem compound CID {cid}"
    if title:
        subject += f" ({title})"

    clauses: list[str] = []
    if iupac_name:
        clauses.append(f"IUPAC name {iupac_name}")
    if formula:
        clauses.append(f"molecular formula {formula}")
    if smiles:
        clauses.append(f"canonical SMILES {smiles}")

    if clauses:
        if len(clauses) == 1:
            body = clauses[0]
        else:
            body = ", ".join(clauses[:-1]) + f", and {clauses[-1]}"
        sentence = f"{subject} has {body}"
    else:
        sentence = subject

    sentence += ", as catalogued in the NCBI PubChem Compound database."
    return sentence


def pubchem_row(rec: dict[str, Any], source_file: str) -> dict[str, Any] | None:
    """One joined CID record -> a finalized ``episteme.articles`` row."""
    cid = rec.get("cid")
    native_id = str(cid) if cid is not None else None
    if not native_id:
        return None  # no native id: caller counts + skips (never synthesize an id)
    lic, lic_url, lic_raw = normalize_license(_PUBCHEM_LICENSE_RAW)
    # GOVERNANCE OVERRIDE (SP4.1 spec 3.4, user decision 2026-09-19): this source's
    # own terms are treated as public domain -> commercial-eligible. Source-anchored
    # on purpose: normalize_license() never returns this for free text. PubChem and
    # ClinVar carry contributor-submitted content with per-record terms; the
    # user accepted that risk. license_raw keeps the real disclaimer text.
    lic = LICENSE_PUBLIC_DOMAIN
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"{SOURCE}:{native_id}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": native_id,
        "pmid": None,
        "pmcid": None,
        "doi": None,
        "title": None,
        "abstract": None,
        "body_text": None,
        "text": _build_text(rec),
        "authors": None,
        "journal": None,
        "year": None,
        "mesh": None,
        "publication_types": None,
        "language": None,
        "license": lic,
        "license_url": lic_url,
        "license_raw": lic_raw,
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


def iter_rows_from_file(
    path: Path,
    *,
    source_file: str | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one ``episteme.articles`` row per CID in one PubChem
    ``CID-SMILES`` primary file (bare ``.tsv``, ``.tsv.gz``, or the real
    ``.gz`` download shape), best-effort-enriched from ``CID-IUPAC`` /
    ``CID-Title`` / ``CID-Mass`` sibling files if present alongside it.

    NOT a streaming read despite the generator shape: ``con.execute(...)`` +
    ``con.fetchall()`` materializes the whole join in memory before the
    first row is yielded (same DuckDB DBAPI limitation as
    ``serialize_chembl.py``'s ``iter_rows_from_file``). Harmless for this
    task's fixture; a real full ``CID-SMILES.gz`` is on the order of 10^8
    rows -- flagged as the same class of named ops concern chembl's task-5
    report raised for its own 10^7-row ``activities`` table, not fixed here.
    """
    source_file = source_file or path.name
    siblings = _find_siblings(path)
    sql, params = _build_query(path, siblings)
    con = duckdb.connect()
    try:
        con.execute(sql, params)
        cols = [d[0] for d in con.description]
        for raw in con.fetchall():
            rec = dict(zip(cols, raw, strict=True))
            row = pubchem_row(rec, source_file)
            if row is None:
                if stats is not None:
                    stats["skipped_no_id"] = stats.get("skipped_no_id", 0) + 1
                continue
            yield row
    finally:
        con.close()


def discover_pubchem_files(raw_dir: Path) -> list[Path]:
    """Discover primary files by a PREFIX-wildcard pattern
    (``CID-SMILES*.gz`` etc.), not an exact-name match: the real download
    always lands as exactly ``CID-SMILES.gz`` (download_pubchem.sh writes to
    a single fixed dest path per source, so a real raw_dir only ever holds
    one), but the wildcard lets a manually-assembled raw_dir (or a unit
    test, mirroring chembl/uniprot's own multi-file ``--max-files`` test
    idiom) hold several distinctly-named primary files side by side, e.g.
    ``CID-SMILES.tsv`` + ``CID-SMILES_2.tsv``. Two files sharing the
    same basename in different directories are both kept: the marker's
    identity is the raw-dir-relative ``input_key``, not the bare basename."""
    patterns = [f"{_PRIMARY_STEM}*{ext}" for ext in _CANDIDATE_EXTS]
    files = discover_input_files(raw_dir, patterns)
    files.sort(key=lambda p: input_key(p, raw_dir))
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror serialize_chembl.py / serialize_uniprot.py's
    ``_best_effort_audit`` shape verbatim, including the settled
    ``event_type="serialize_commit"`` ruling (NOT ``"extract_commit"`` --
    settled by Tasks 4/5/6, not re-litigated here).

    One chained audit row on a fresh connection, degrading to the file-only
    mirror when no DB is reachable. Never raises -- ``mark_success`` has
    already run for this file."""
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
                _LOG.warning(
                    "audit mirror_only fallback also failed for %s", basename, exc_info=True
                )


def process_one(
    path: Path,
    *,
    processed_dir: Path,
    raw_dir: Path,
    force: bool,
) -> dict[str, Any]:
    basename = input_key(path, raw_dir)
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    iter_stats: dict[str, int] = {}
    try:
        for row in iter_rows_from_file(path, source_file=basename, stats=iter_stats):
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
        stats.update(iter_stats)
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
            f"status={result.get('extract_status_counts')}",
            file=sys.stderr,
        )
        if result.get("skipped_no_id"):
            print(f"  skipped_no_id={result['skipped_no_id']}", file=sys.stderr)
    else:
        print(f"FAIL {basename}: {result.get('error')}", file=sys.stderr)


def serialize_pubchem(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``CID-SMILES*`` primary files under ``raw_dir``
    (best-effort enriched from sibling ``CID-IUPAC``/``CID-Title``/
    ``CID-Mass`` files in the same directory) into staging shards + ops
    markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_pubchem_files(raw_dir)
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
    files = discover_pubchem_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no {_PRIMARY_STEM}* files under {raw_dir}", file=sys.stderr)
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
        prog="python -m episteme.data.pubchem.serialize_pubchem",
        description="PubChem compound_extras property TSVs to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "pubchem")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered CID-SMILES* primary files (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_pubchem(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no {_PRIMARY_STEM}* files under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
