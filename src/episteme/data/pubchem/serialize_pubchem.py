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
joined in by a streaming merge join (best-effort, NOT required -- a primary file
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

Bounded units of work (pubchem sub-project 2, spec 2026-10-26 Sec 4.2-4.3):
one primary file is still one unit of work, but its output is a sequence of
parts numbered from 1. A part holds at most ``part_rows`` rows and at most
``part_bytes`` estimated bytes (sum of each row's UTF-8 ``text`` length plus
``PART_ROW_OVERHEAD_BYTES``): a row that would take the part over the byte
cap ends the part and starts the next one. The one exception: a single row
larger than ``part_bytes`` goes alone in its own part, which is then over the
byte cap (a row cannot be split; ``gate_report`` leaves single-row parts out
of its byte maximum). The caps come from the runtime profile (``_part_caps``).
Part ``n`` has the ``source_file``, shard stem and success marker
``<input_key>__partNNNNN`` (``part_source_file``; at least 5 digits); its
marker records ``first_cid`` / ``last_cid`` and the caps, and it gets one
``serialize_commit`` audit row, written before its marker. After the last
part the primary file gets its own success marker (``parts``,
``parts_list``, ``inputs``, total ``n_rows``); it has no shard and no audit
row of its own. A restart does not count rows: it keeps the longest prefix of
parts whose markers and shards exist and whose CID ranges follow each other,
deletes every part above it (markers first, then shards), passes over every
record up to the last kept CID without building rows, and writes the next
parts. Changed caps log a warning; no CID is lost or written twice, because
the boundary is a CID. Each part marker also records ``inputs``, the
``[name, size, mtime_ns]`` of the primary file and of each sibling used; when
the kept parts' ``inputs`` differ from the current files (or a marker has
none), the run logs a warning and drops every part of the unit, as
``--force`` does. The primary marker records ``inputs`` and ``parts_list``
(the names of the unit's parts) too. A run honours it (skips the unit) only
when it has this format and its ``inputs`` equal the current files; else the
run logs a warning and writes the unit again from part 1, as ``--force``
does (a marker from before parts, or a new download after the unit
completed). ``--force`` deletes the primary marker and all parts of the unit
first.

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
from episteme.data import staging_writer  # noqa: E402
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
    failed_marker_path,
    input_key,
    is_success,
    load_success_marker_path,
    mark_failed,
    mark_success,
    ops_root,
    success_marker_path,
    write_marker,
    write_run_manifest,
)
from episteme.data.gz import open_gz  # noqa: E402
from episteme.data.parallel import record_first_failure, run_per_file, skip_files  # noqa: E402
from episteme.data.staging_writer import write_rows  # noqa: E402
from episteme.ops.profile import PART_ROW_OVERHEAD_BYTES, get_profile  # noqa: E402
from episteme.ops.retry import TransientUnitError, is_transient  # noqa: E402
from episteme.ops.skip import add_skip_args, check_skip_args  # noqa: E402
from episteme.ops.stop import stage_guard  # noqa: E402

_LOG = logging.getLogger(__name__)

SOURCE = "pubchem"

# Batch size is module-level (read at call time) so tests can shrink it.
_WRITE_BATCH_ROWS = staging_writer.WRITE_BATCH_ROWS

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
# The text is constant, so it goes through normalize_license once, here, not once
# per row (the call cost ~186 us of ~289 us per row). pubchem_row uses
# license_url / license_raw from it and applies the governance override.
_PUBCHEM_LICENSE = normalize_license(_PUBCHEM_LICENSE_RAW)

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors extract_pubmed.py / serialize_chembl.py / serialize_uniprot.py.
_AUDIT_LOCK = threading.Lock()

# The PRIMARY file: one row per CID present here drives the whole run (see
# module docstring's unit-of-work decision). Candidate extensions cover the
# real download shape (bare ``.gz``, no ``.tsv`` in the name) and the
# unit-test fixture / a manually-supplied ``.tsv`` or ``.tsv.gz`` shape.
_PRIMARY_STEM = "CID-SMILES"
_CANDIDATE_EXTS = (".gz", ".tsv.gz", ".tsv")

# SIBLING files: best-effort enrichment, looked up by exact stem in the same
# directory as the primary file. Column shapes verified against a real
# download of each file (README-Extras + inspecting real Compound/Extras
# content directly), NOT guessed:
#   CID-IUPAC.gz  -- CID, tab, IUPAC name
#   CID-Title.gz  -- CID, tab, compound-summary-page title
#   CID-Mass.gz   -- CID, tab, formula, tab, monoisotopic mass, tab, exact mass
# Column order is the file's field order; n_fields = len(columns) - 1. The types
# describe the data only: the reader keeps every field as the text it read.
_SIBLING_DEFS: dict[str, dict[str, Any]] = {
    "iupac_name": {
        "stem": "CID-IUPAC",
        "columns": {"cid": "BIGINT", "iupac_name": "VARCHAR"},
    },
    "title": {
        "stem": "CID-Title",
        "columns": {"cid": "BIGINT", "title": "VARCHAR"},
    },
    "formula": {
        "stem": "CID-Mass",
        "columns": {
            "cid": "BIGINT",
            "formula": "VARCHAR",
            "monoisotopic_mass": "DOUBLE",
            "exact_mass": "DOUBLE",
        },
    },
}


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


class PubchemInputError(ValueError):
    """A permanent input error: a PubChem property file is not what the
    streaming merge join needs (integer CIDs, sorted, one row per CID)."""


def _open_binary(path: Path):
    if path.suffix == ".gz":
        return open_gz(path, "rb")
    return open(path, "rb")  # noqa: SIM115


def _strip_eol(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return line[:-2]
    if line.endswith(b"\n"):
        return line[:-1]
    return line


def _iter_cid_raw(
    path: Path, stats: dict[str, int] | None = None
) -> Iterator[tuple[int, bytes, list[str]]]:
    r"""Yield ``(cid, line, parts)`` for each line of one CID-sorted file.

    The file is read in binary mode and only ``b"\n"`` ends a line, so a lone
    ``\r`` inside a field stays in the field. ``line`` is the line's bytes as
    read (line ending kept; the slice cut writes it unchanged); ``parts`` is
    the line without its ``\n`` or ``\r\n`` ending, decoded as UTF-8 (an
    invalid byte becomes U+FFFD) and split on tab. A line with an empty CID is
    counted in ``stats["skipped_no_id"]`` and skipped. A CID that is not an
    integer, or that is not strictly greater than the one before, raises
    ``PubchemInputError``.
    """
    prev: int | None = None
    with _open_binary(path) as fh:
        for lineno, line in enumerate(fh, start=1):
            parts = _strip_eol(line).decode("utf-8", errors="replace").split("\t")
            raw = parts[0]
            if not raw:
                if stats is not None:
                    stats["skipped_no_id"] = stats.get("skipped_no_id", 0) + 1
                continue
            try:
                cid = int(raw)
            except ValueError:
                raise PubchemInputError(
                    f"{path.name} line {lineno}: CID {raw!r} is not an integer"
                ) from None
            if prev is not None and cid <= prev:
                raise PubchemInputError(
                    f"{path.name} line {lineno}: CID {cid} after {prev}: "
                    "the file is not sorted by CID with one row per CID"
                )
            prev = cid
            yield cid, line, parts


def _iter_cid_lines(
    path: Path, n_fields: int, stats: dict[str, int] | None = None
) -> Iterator[tuple[int, list[str | None]]]:
    """Yield ``(cid, fields)`` for each line of one CID-sorted property file.

    ``fields`` has exactly ``n_fields`` items (padded or truncated); an empty
    field is ``None``. Empty and invalid CIDs follow ``_iter_cid_raw``.
    """
    for cid, _line, parts in _iter_cid_raw(path, stats):
        fields: list[str | None] = [(f or None) for f in parts[1 : 1 + n_fields]]
        fields.extend([None] * (n_fields - len(fields)))
        yield cid, fields


def iter_merged_records(
    primary_path: Path,
    sibling_paths: dict[str, Path],
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Merge-join the primary CID file with the sibling files, one pass.

    All files are sorted by CID, so each sibling keeps one cursor that moves
    forward while its CID is lower than the primary CID. Memory is one line
    per file. A sibling CID absent from the primary is ignored; a primary CID
    absent from a sibling gets ``None`` for that sibling's fields. After the
    last primary record, every sibling is read to its end, so an order error
    anywhere in a sibling raises ``PubchemInputError``.

    The masses of CID-Mass stay the raw strings as read (or ``None``): the row
    text does not use them.
    """
    # The field names of each sibling, computed once (not once per record).
    names: dict[str, list[str]] = {
        key: list(spec["columns"])[1:] for key, spec in _SIBLING_DEFS.items()
    }
    cursors: dict[str, Iterator[tuple[int, list[str | None]]]] = {}
    heads: dict[str, tuple[int, list[str | None]] | None] = {}
    primary = _iter_cid_lines(primary_path, 1, stats)
    try:
        for key, path in sibling_paths.items():
            cursors[key] = _iter_cid_lines(path, len(names[key]))
            heads[key] = next(cursors[key], None)
        for cid, (smiles,) in primary:
            rec: dict[str, Any] = {"cid": cid, "smiles": smiles}
            for key, fields in names.items():
                values: list[str | None] | None = None
                if key in cursors:
                    head = heads[key]
                    while head is not None and head[0] < cid:
                        head = next(cursors[key], None)
                    heads[key] = head
                    if head is not None and head[0] == cid:
                        values = head[1]
                if values is None:
                    for name in fields:
                        rec[name] = None
                else:
                    rec.update(zip(fields, values, strict=True))
            yield rec
        # The primary has ended. Read every sibling to its end through the
        # validating reader: a line behind a cursor that is ahead, or a
        # backward CID in a tail beyond the primary's last CID, raises
        # PubchemInputError here, so the unit cannot succeed on such a file.
        for cursor in cursors.values():
            for _ in cursor:
                pass
    finally:
        for it in (primary, *cursors.values()):
            with contextlib.suppress(Exception):
                it.close()  # type: ignore[attr-defined]


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
    _lic, lic_url, lic_raw = _PUBCHEM_LICENSE
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

    A streaming merge join: one line per file is in memory at a time.
    """
    source_file = source_file or path.name
    siblings = _find_siblings(path)
    with contextlib.closing(iter_merged_records(path, siblings, stats)) as records:
        for rec in records:
            row = pubchem_row(rec, source_file)
            if row is None:
                if stats is not None:
                    stats["skipped_no_id"] = stats.get("skipped_no_id", 0) + 1
                continue
            yield row


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
    mirror when no DB is reachable. Never raises. ``process_one`` calls it
    for a part BEFORE that part's ``mark_success`` (a stop between the two
    gives a duplicate audit row on the rerun, never a missing one)."""
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


def part_source_file(key: str, n: int) -> str:
    """The ``source_file`` (and shard stem, and marker name) of part ``n`` of
    the unit whose ``input_key`` is ``key``."""
    return f"{key}__part{n:05d}"


def _part_caps() -> tuple[int, int]:
    """``(part_rows, part_bytes)`` from the runtime profile (a test seam)."""
    profile = get_profile()
    return profile.part_rows, profile.part_bytes


def _staging_dir(processed_dir: Path) -> Path:
    return Path(processed_dir) / "staging" / SOURCE


def _part_shards(processed_dir: Path, psf: str) -> list[Path]:
    """Every staging file of one part: ``{psf}.parquet``, ``{psf}.jsonl`` and a
    leftover ``{psf}.parquet.tmp`` (``staging_writer._out_path`` names a shard
    ``{stem}{suffix}``; each ``write_rows`` call writes one file)."""
    d = _staging_dir(processed_dir)
    if not d.is_dir():
        return []
    return sorted(d.glob(glob.escape(psf) + ".*"))


def _read_part_marker(processed_dir: Path, psf: str) -> dict[str, Any] | None:
    p = success_marker_path(processed_dir, SOURCE, psf)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stats = payload.get("stats") if isinstance(payload, dict) else None
    return stats if isinstance(stats, dict) else None


def read_primary_marker(processed_dir: Path, key: str) -> dict[str, Any] | None:
    """The stats of the primary success marker of unit ``key`` when it has the
    parts format, else ``None`` (no marker, unreadable, or a marker written
    before parts). The parts format: ``parts`` is an int, ``parts_list`` is a
    list of ``parts`` part names, each ``part_source_file(key, n)`` for
    n = 1..parts, and ``inputs`` is a list (the input fingerprint)."""
    st = _read_part_marker(processed_dir, key)
    if st is None:
        return None
    parts, names, inputs = st.get("parts"), st.get("parts_list"), st.get("inputs")
    if not isinstance(parts, int) or isinstance(parts, bool) or parts < 0:
        return None
    if names != [part_source_file(key, n) for n in range(1, parts + 1)]:
        return None
    if not isinstance(inputs, list):
        return None
    return st


def _kept_parts(processed_dir: Path, key: str) -> list[dict[str, Any]]:
    """The stats of parts 1..j: the longest prefix whose markers are readable,
    whose shard exists, and whose CID ranges follow each other."""
    kept: list[dict[str, Any]] = []
    prev_last: int | None = None
    while True:
        psf = part_source_file(key, len(kept) + 1)
        st = _read_part_marker(processed_dir, psf)
        if st is None:
            break
        if not any(not p.name.endswith(".tmp") for p in _part_shards(processed_dir, psf)):
            break
        first, last = st.get("first_cid"), st.get("last_cid")
        if not (isinstance(first, int) and isinstance(last, int) and first <= last):
            break
        if prev_last is not None and first <= prev_last:
            break
        kept.append(st)
        prev_last = last
    return kept


def _input_fingerprint(path: Path, siblings: dict[str, Path]) -> list[list[Any]]:
    """``[name, size, mtime_ns]`` of the primary file, then of each sibling
    used, sorted by name. Lists, not tuples, so it compares equal to the copy
    read back from a part marker's JSON."""
    files = [path, *sorted(siblings.values(), key=lambda p: p.name)]
    out: list[list[Any]] = []
    for p in files:
        st = p.stat()
        out.append([p.name, st.st_size, st.st_mtime_ns])
    return out


# The retire pending file: the part names (source_file values) whose parts
# serialize deleted after they were loaded. A JSON list, sorted, no duplicates,
# written atomically. retire.py reads it after a good load.
_PENDING_LOCK = threading.Lock()


def retire_pending_path(processed_dir: Path) -> Path:
    return ops_root(processed_dir, SOURCE) / "retire_pending.json"


def read_retire_pending(processed_dir: Path) -> list[str]:
    """The pending part names; ``[]`` when the file does not exist. A file
    that is not a JSON list of strings raises ``ValueError`` (never guess
    which rows to delete)."""
    p = retire_pending_path(processed_dir)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    data = json.loads(text)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError(f"{p}: not a JSON list of part names")
    return data


def write_retire_pending(processed_dir: Path, names: list[str]) -> None:
    """Replace the pending file (atomic: temp file, fsync, rename)."""
    write_marker(retire_pending_path(processed_dir), sorted(set(names)))  # type: ignore[arg-type]


def add_retire_pending(processed_dir: Path, names: list[str]) -> None:
    """Add ``names`` to the pending file. Serialized across this process's
    worker threads (a read-modify-write); processes are serialized by the
    heavy lock."""
    with _PENDING_LOCK:
        write_retire_pending(processed_dir, [*read_retire_pending(processed_dir), *names])


def _delete_parts_above(processed_dir: Path, key: str, j: int) -> None:
    """Delete the markers, then the shards, of every part of ``key`` above
    ``j``. Markers go first: a stop between the two steps leaves a shard
    without a marker (deleted on the next run), never a marker without data.

    The load-success markers of those parts go too. The loader names them by
    shard file name (``load_success/<psf>.parquet.ok``) and skips a shard
    whose marker exists, so a part rewritten with a new CID range must lose
    its load marker or its new rows never reach the database.

    A deleted part that has a load marker was loaded: its rows are in the
    database. Its name goes into the retire pending file FIRST, before any
    delete, so a stop at any point here loses no name (``retire.py`` deletes
    the rows of the pending names after the next good load)."""
    # 5 digits or more: part_source_file pads to 5 and writes 6+ from 100000.
    pattern = re.compile(re.escape(key) + r"__part(\d{5,})\.")
    prefix = glob.escape(f"{key}__part") + "*"
    load_dir = load_success_marker_path(processed_dir, SOURCE, "x").parent
    dirs = [
        success_marker_path(processed_dir, SOURCE, "x").parent,
        failed_marker_path(processed_dir, SOURCE, "x").parent,
        load_dir,
    ]
    loaded: list[str] = []
    if load_dir.is_dir():
        for p in load_dir.glob(prefix):
            m = pattern.match(p.name)
            if m and int(m.group(1)) > j:
                loaded.append(f"{key}__part{m.group(1)}")
    if loaded:
        add_retire_pending(processed_dir, loaded)
    for group in (dirs, [_staging_dir(processed_dir)]):
        for d in group:
            if not d.is_dir():
                continue
            for p in d.glob(prefix):
                m = pattern.match(p.name)
                if m and int(m.group(1)) > j:
                    p.unlink(missing_ok=True)


def process_one(
    path: Path,
    *,
    processed_dir: Path,
    raw_dir: Path,
    force: bool,
) -> dict[str, Any]:
    """Serialize one primary file as a sequence of bounded parts.

    See the module docstring, "Bounded units of work": each part is a shard
    plus a success marker that records its CID range; a restart keeps the
    longest chain of complete parts and resumes after its last CID.
    """
    basename = input_key(path, raw_dir)
    t0 = time.time()
    cap_rows, cap_bytes = _part_caps()
    status_counts: Counter[str] = Counter()
    n_rows_total = 0
    n_rows_new = 0
    iter_stats: dict[str, int] = {}
    current_part: int | None = None
    try:
        siblings = _find_siblings(path)
        fingerprint = _input_fingerprint(path, siblings)
        rebuild = force
        if not force and is_success(processed_dir, SOURCE, basename):
            primary = read_primary_marker(processed_dir, basename)
            if primary is not None and primary["inputs"] == fingerprint:
                return {"source_file": basename, "skipped": True, "reason": "success_marker"}
            # A marker from before parts, or one whose inputs differ from the
            # current files (a new download): the unit is not done. Write it
            # again from part 1, as --force does.
            _LOG.warning(
                "%s: %s; dropping all parts and writing the unit again",
                basename,
                "the primary success marker has no parts format"
                if primary is None
                else "an input file changed since the unit completed "
                "(name, size or mtime of the primary or a sibling)",
            )
            rebuild = True
        if rebuild:
            # The primary marker goes first: a stop during the rebuild leaves
            # no marker with an old parts_list for the retire step to trust.
            success_marker_path(processed_dir, SOURCE, basename).unlink(missing_ok=True)
        kept = [] if rebuild else _kept_parts(processed_dir, basename)
        if any(st.get("inputs") != fingerprint for st in kept):
            # A kept part from other input files (a new download, a sibling
            # added or gone, or a marker without a fingerprint) cannot be
            # resumed: drop every part of the unit, as --force does.
            _LOG.warning(
                "%s: an input file changed since the kept parts were written "
                "(name, size or mtime of the primary or a sibling); dropping all "
                "%d kept parts and writing the unit again",
                basename,
                len(kept),
            )
            kept = []
        j = len(kept)
        _delete_parts_above(processed_dir, basename, j)
        if any((st.get("part_rows"), st.get("part_bytes")) != (cap_rows, cap_bytes) for st in kept):
            _LOG.warning(
                "%s: the part caps changed since the kept parts were written "
                "(kept %d parts; caps now part_rows=%d part_bytes=%d); the kept parts "
                "keep their CID ranges and the new parts use the new caps",
                basename,
                j,
                cap_rows,
                cap_bytes,
            )
        for st in kept:
            n_rows_total += int(st.get("n_rows") or 0)
            status_counts.update(st.get("extract_status_counts") or {})
        resume_cid: int | None = kept[-1]["last_cid"] if kept else None

        records = iter_merged_records(path, siblings, iter_stats)
        with contextlib.closing(records):
            # Cheap skip: records in the kept range build no row.
            pending_src = (rec for rec in records if resume_cid is None or rec["cid"] > resume_cid)
            n = j
            current_part = n + 1
            pending = next(pending_src, None)
            while pending is not None:
                n += 1
                current_part = n
                psf = part_source_file(basename, n)
                part: dict[str, Any] = {
                    "rows": 0,
                    "est_bytes": 0,
                    "first_cid": None,
                    "last_cid": None,
                    "counts": Counter(),
                }

                def _part_rows(psf: str = psf, part: dict[str, Any] = part):
                    nonlocal pending
                    while pending is not None:
                        rec = pending
                        row = pubchem_row(rec, psf)
                        if row is None:
                            iter_stats["skipped_no_id"] = iter_stats.get("skipped_no_id", 0) + 1
                            pending = next(pending_src, None)
                            continue
                        text = row.get("text")
                        row_est = (
                            len(text.encode("utf-8")) if text else 0
                        ) + PART_ROW_OVERHEAD_BYTES
                        # A part holds at most part_bytes: a row that does not
                        # fit ends the part and stays pending for the next part
                        # (rebuilt there with that part's source_file). A single
                        # row over the cap goes alone in its own part.
                        if part["rows"] and part["est_bytes"] + row_est > cap_bytes:
                            return
                        pending = None
                        part["rows"] += 1
                        part["est_bytes"] += row_est
                        if part["first_cid"] is None:
                            part["first_cid"] = rec["cid"]
                        part["last_cid"] = rec["cid"]
                        part["counts"][str(row.get("extract_status"))] += 1
                        yield row
                        # The row cap check comes before the next pull, so a
                        # part never reads a record past its last row.
                        if part["rows"] >= cap_rows:
                            return
                        pending = next(pending_src, None)

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
                if part["rows"] == 0:  # every remaining record was dropped
                    n -= 1
                    break
                part_stats = {
                    "n_rows": part["rows"],
                    "extract_status_counts": dict(part["counts"]),
                    "first_cid": part["first_cid"],
                    "last_cid": part["last_cid"],
                    "part_rows": cap_rows,
                    "part_bytes": cap_bytes,
                    "est_bytes": part["est_bytes"],
                    "inputs": fingerprint,
                    "write": write_info,
                }
                # The audit row comes before the marker: a stop between the
                # two leaves an audited part without a marker, which the next
                # run writes and audits again (a duplicate audit row), never a
                # marked part without an audit row.
                _best_effort_audit(psf, part["rows"])
                mark_success(processed_dir, SOURCE, psf, stats=part_stats)
                n_rows_new += part["rows"]
                n_rows_total += part["rows"]
                status_counts.update(part["counts"])
                current_part = n + 1
                if pending is None:  # else a record that did not fit waits
                    pending = next(pending_src, None)

        stats: dict[str, Any] = {
            "n_rows": n_rows_total,
            "parts": n,
            "parts_list": [part_source_file(basename, i) for i in range(1, n + 1)],
            "inputs": fingerprint,
            "parts_written": n - j,
            "parts_kept": j,
            "part_rows": cap_rows,
            "part_bytes": cap_bytes,
            "extract_status_counts": dict(status_counts),
            "elapsed_sec": round(time.time() - t0, 4),
        }
        stats.update(iter_stats)
        mark_success(processed_dir, SOURCE, basename, stats=stats)
        return {"source_file": basename, "skipped": False, "ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        if is_transient(e):
            raise TransientUnitError(basename, repr(e)) from None
        mark_failed(
            processed_dir,
            SOURCE,
            basename,
            error_class=type(e).__name__,
            message=str(e),
            stats={"n_rows_partial": n_rows_new, "failed_part": current_part},
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
    skip_units: list[str] | None = None,
    skip_reason: str | None = None,
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
    files = skip_files(SOURCE, "serialize", files, raw_dir, skip_units, skip_reason)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    t0 = time.time()

    results = run_per_file(
        process_one,
        files,
        workers=workers,
        executor="thread",
        on_result=_print_verbose if verbose else None,
        raw_dir=raw_dir,
        processed_dir=processed_dir,
        force=force,
    )
    record_first_failure(SOURCE, "serialize", results)

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


def _main(argv: list[str] | None = None) -> int:
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
    add_skip_args(p)
    args = p.parse_args(argv)
    check_skip_args(p, args)
    if args.report and args.skip_unit:
        p.error("--skip-unit cannot be used with --report")

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
        skip_units=args.skip_unit,
        skip_reason=args.reason,
    )

    # inputs == 0 after --skip-unit dropped every file is a clean run (R8).
    if res["inputs"] == 0 and not args.skip_unit:
        print(f"ERROR: no {_PRIMARY_STEM}* files under {raw_dir}", file=sys.stderr)
        return 1

    print(f"done inputs={res['inputs']} ok={res['ok']} failed={res['failed']} rows={res['rows']}")
    return 1 if res["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    # Extract/serialize hold no long DB statement: only the stop handlers and
    # the parent watchdog apply (conn_ref returns None).
    with stage_guard(lambda: None, lock_stage="serialize", argv=argv, source=SOURCE) as g:
        rc = _main(argv)
        g.exit_code(rc)
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
