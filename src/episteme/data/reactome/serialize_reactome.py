#!/usr/bin/env python3
"""Structured serializer: Reactome -- parse Reactome's flat current-release
directory (``download_reactome.sh``'s real artifact shape -- see module
docstring's "Unit-of-work" section) into the unified ``episteme.articles``
row schema (contract v1.4).

Unit-of-work decision (SP4 Task 9 -- documented here per the controller's
explicit instruction, mirroring pubchem task-7's multi-file writeup):
``download_reactome.sh`` (no MODE -- fetches EVERYTHING matching
``\\.(txt|tsv|csv|owl|sbml|zip|gz|json|graphml)(\\.gz)?$`` from Reactome's real
flat ``https://reactome.org/download/current/`` directory listing, confirmed
by fetching that listing directly at implementation time, 2026-09-16/17 --
NOT by running ``--max-files N``, see "Real end-to-end" note below) ships
~80 files. Three were selected as this module's unit of work, confirmed
against real downloaded bytes, not guessed:

  * ``ReactomePathways.txt`` (PRIMARY) -- pathway_id / name / species, NO
    header, 23,603 real rows spanning 16 species. This is the brief's own
    named "core reference table" and drives iteration -- one row per
    pathway_id, one success/failure marker per primary file (Sec 4.11's
    "unit of work = one input file"), confirmed pathway_id-unique in the
    real file (``cut -f1 ReactomePathways.txt | sort | uniq -d`` -> empty).

  * ``pathway2summation.txt`` (SIBLING 1: pathway description prose) --
    Identifier / Name / Summation, HAS a header, real free-text literature
    summaries. Confirmed real-data findings, NOT assumed:
      - HUMAN-ONLY: all 2,883 distinct Identifiers in the real file are
        exactly the 2,883 Homo sapiens rows in ReactomePathways.txt (1:1
        coverage, confirmed by set difference -- zero human pathways
        lacking a summation, zero summation rows for a non-R-HSA id). Every
        non-human primary row (the other 20,720 pathways) is therefore
        summation-less BY CONSTRUCTION, not a coverage gap.
      - ONE real duplicate Identifier: ``R-HSA-166016`` ("Toll Like
        Receptor 4 (TLR4) Cascade") has TWO summation rows with genuinely
        different prose (two distinct literature-review paragraphs, not a
        parsing artifact). A naive LEFT JOIN would fan this pathway's row
        out to 2 physical result rows sharing one ``id`` -- the same class
        of bug clinvar task-8's assembly-dedup finding guarded against.
        Deduped in SQL via ``QUALIFY ROW_NUMBER() OVER (PARTITION BY
        pathway_id ORDER BY summation) = 1`` (alphabetical-first summation
        text, a deterministic tiebreak -- mirrors clinvar's own reasoning:
        determinism/testability is what matters here, not which of two
        equally-valid real paragraphs survives).
      - ONE real malformed row: ``R-HSA-212436`` ("Generic Transcription
        Pathway")'s Summation field contains a literal embedded raw TAB
        character (not CSV-quoted), splitting that one physical line into 4
        tab-fields instead of 3 and making DuckDB's CSV sniffer refuse the
        whole file outright (``InvalidInputException: ... sniffing file
        ... columns: 3 ... sniffer: 4``) unless ``ignore_errors=true`` is
        set. With it, DuckDB silently drops exactly that 1 malformed row
        (confirmed: 2,884 real data rows in the raw file -> 2,883 rows
        survive ``ignore_errors=true``) and parses the other 2,883 rows
        (including the ``R-HSA-166016`` duplicate pair) correctly. This
        module accepts the 1-row drop as a real, documented data-quality
        gap (same posture as clinvar's "-" sentinel / uniprot's ``.dat``
        gap) -- R-HSA-212436 still serializes successfully, just without
        its summation enrichment (falls back to name+species text, exactly
        like any other summation-less pathway).
      - ``quote=''`` is required (mirrors clinvar's ``_QUERY``): summation
        prose contains raw double-quote characters (99 occurrences
        confirmed in the real file) that DuckDB's default quote-char
        handling would otherwise mis-parse as CSV quoting.

  * ``UniProt2Reactome.txt`` (SIBLING 2: gene/protein-membership prose) --
    uniprot_id / pathway_id / url / pathway_name / evidence_code / species,
    NO header, 6 columns confirmed consistent across the real file. This is
    Reactome's LOWEST-LEVEL annotation file (a protein is listed against the
    most specific pathway it directly participates in, not propagated to
    parent/ancestor pathways) -- confirmed by real measurement: it covers
    17,930 of the 23,603 real primary pathway_ids (~76%). The alternative,
    ``UniProt2Reactome_All_Levels.txt``, propagates annotations up the
    pathway hierarchy (higher coverage) but is ~2.7x the download size
    (117,823,518 vs. 43,041,375 bytes, both confirmed via a real HEAD/size
    probe at implementation time) for a coverage gain this task's "smallest
    set that gets you real gene-membership prose" brief instruction does not
    justify -- 76% direct-annotation coverage on the plain file IS real
    gene/protein-membership prose for the large majority of pathways.
    NOT joined 1:1 like pubchem's siblings: a pathway_id maps to MANY
    uniprot_ids, so this sibling is pre-aggregated in a CTE (``DISTINCT``
    pair dedup, since the same uniprot_id/pathway_id pair can repeat across
    the file with different evidence codes; ``ROW_NUMBER() OVER (PARTITION
    BY pathway_id ORDER BY uniprot_id)`` caps the rendered sample at
    ``_MAX_PROTEIN_SAMPLE`` accessions; a separate ``COUNT(*)`` over the
    full distinct-pair set carries the true total so the sentence can say
    "among N annotated in total" when the sample is capped) BEFORE being
    LEFT JOINed onto the primary read -- a genuinely new wrinkle vs.
    pubchem's plain 1:1 sibling joins, not reused unmodified from that
    template.

Files considered and NOT used, with reasoning: ``Ensembl2Reactome.txt`` /
``NCBI2Reactome.txt`` are alternative gene-identifier-space siblings
covering the same membership relationship as ``UniProt2Reactome.txt`` --
redundant with it for this task's purposes, not both needed (the brief asks
for "the smallest set", not every mapping file). ``gene_association.reactome
.gz`` (GAF format) carries GO terms with Reactome pathways only as a
DB:Reference citation column, not a direct membership table, and needs
GAF-specific parsing (bang-comment header lines, embedded
``REACTOME:R-HSA-xxx`` string splitting) for no benefit over the
purpose-built ``*2Reactome.txt`` files -- out of scope. ``ReactomePathways
Relation.txt`` (pathway hierarchy edges) and ``pathway2summation``'s sibling
``LowerLevelPathway2Topic*.txt`` were considered as a possible "roll up
child-pathway summations to parent" enrichment but add a recursive-join
layer this task's brief does not ask for -- flagged as a possible follow-up
enrichment, not attempted (same discipline as pubchem's
``CID-Synonym``-aggregation scope cut).

Reactome pathway records carry no bibliographic shape: ``title``/``journal``/
``year``/``authors`` are always ``None`` (mirrors chembl/uniprot/pubchem/
clinvar's convention). ``container_id``/``book_meta`` are ``None`` on every
row.

``text`` template (SP4 task-9 brief's own "combining pathway description with
... gene/protein-membership prose" framing): "Reactome pathway {name}
({species}). {summation}" when a summation is present, else just the
subject sentence, followed by "Known protein participants include {sample},
among {n} annotated in total." (or without the "among N" clause when the
full distinct-protein count is within the rendered sample) when protein
enrichment is present, and closing with ", as catalogued in the Reactome
pathway database." always.

MIN_OK_TEXT_LEN (200 chars) sanity-check against real fixture rows (see
task-9-report.md for the exact character counts): every summation-backed
row (human pathways, real Summation text 614-2,431 chars in this module's
own fixture) clears 200 chars by a wide margin -> ``extract_status="ok"``.
A non-human pathway with NO summation (summation-less by construction, see
above) and either a short protein-accession sample or none at all lands
well under 200 chars -> ``extract_status="partial"`` -- honest, not a bug,
mirroring every prior SP4 structured source's near-boundary finding for
compact/sparse records. No fixture row lands "empty": every primary row
carries at minimum a non-empty "Reactome pathway {name} ({species})"
sentence.

Licence: Reactome's own real "License Agreement" page (fetched directly from
https://reactome.org/license at implementation time, 2026-09-17), Section 1c
("Data"): "All data in the Reactome database and files derived from that
data are licensed under the Creative Commons Public Domain Dedication (CC0).
User may copy, modify, and distribute these data, even for commercial
purposes, without asking for permission. Attribution is encouraged but not
required." This DOES contain the literal ``CC0`` token normalize_license's
existing CC0 arm (``re.search(r"\\bCC0\\b", u) or "CC-0" in u``) matches on
-- confirmed by running the real fetched text through
``article_schema.normalize_license`` directly, NOT assumed from the brief's
own guess. Resolves to ``license="CC0"`` -> ``subset_from_license("CC0")``
== ``"commercial"``, exactly as the brief predicted. Unlike pubchem/clinvar
(both of which flagged a licence-resolution GAP against their briefs' own
guesses), this task's real licence confirmation MATCHES the brief -- no gap
to flag, no code change made or needed (``article_schema.py`` is NOT touched
by this task's diff).

Importable core: ``serialize_reactome(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5):
  python -m episteme.data.reactome.serialize_reactome \\
    --raw-dir ./01_raw/reactome \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    [--force]
"""

from __future__ import annotations

import argparse
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

SOURCE = "reactome"

# Reactome's real "License Agreement" page, Section 1c ("Data"), fetched
# directly from https://reactome.org/license at implementation time
# (2026-09-17) -- contains the literal "CC0" token. Run through the ordinary
# article_schema.normalize_license()/subset_from_license() machinery (NOT a
# hardcode) -- confirmed it hits the existing CC0 arm. See module docstring's
# "Licence" section for the confirmed CC0 -> commercial resolution (matches
# the brief's own prediction, unlike pubchem/clinvar's flagged gaps).
_REACTOME_LICENSE_RAW = (
    "Data: All data in the Reactome database and files derived from that "
    "data are licensed under the Creative Commons Public Domain Dedication "
    "(CC0). User may copy, modify, and distribute these data, even for "
    "commercial purposes, without asking for permission. Attribution is "
    "encouraged but not required."
)

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors extract_pubmed.py / serialize_chembl.py / serialize_pubchem.py
# / serialize_clinvar.py.
_AUDIT_LOCK = threading.Lock()

# The PRIMARY file: one row per pathway_id present here drives the whole run
# (see module docstring's unit-of-work decision). Candidate extensions cover
# the real download shape (bare ``.txt``, no compression on this file in a
# real fetch) and a manually-supplied ``.tsv`` fixture shape. Deliberately
# NOT a generic ``*.tsv`` fallback (unlike clinvar's single-fixture-file
# idiom) -- this task's fixture dir holds THREE distinctly-named files, and a
# generic pattern would make every one of them match as an independent
# "primary", breaking _find_siblings' same-directory sibling lookup.
_PRIMARY_STEM = "ReactomePathways"
_CANDIDATE_EXTS = (".txt", ".txt.gz", ".gz", ".tsv")
_PRIMARY_COLUMNS = {"pathway_id": "VARCHAR", "name": "VARCHAR", "species": "VARCHAR"}

# SIBLING 1: pathway-description prose. Column shape + header=true + the
# real duplicate-id / malformed-row findings are documented in the module
# docstring's "Unit-of-work decision" section.
_SUMMATION_STEM = "pathway2summation"
_SUMMATION_COLUMNS = {"pathway_id": "VARCHAR", "name": "VARCHAR", "summation": "VARCHAR"}

# SIBLING 2: gene/protein-membership prose (pre-aggregated, see module
# docstring). Column shape confirmed against real UniProt2Reactome.txt bytes.
_PROTEIN_STEM = "UniProt2Reactome"
_PROTEIN_COLUMNS = {
    "uniprot_id": "VARCHAR",
    "pathway_id": "VARCHAR",
    "url": "VARCHAR",
    "pathway_name": "VARCHAR",
    "evidence_code": "VARCHAR",
    "species": "VARCHAR",
}
# Cap on the number of UniProt accessions rendered in the sentence -- the
# real distinct-protein count per pathway can run into the hundreds (a
# well-studied pathway like TLR4 cascade has 18 in this module's own
# fixture; large real pathways have far more). The full distinct count is
# still carried (via the ``n_proteins`` aggregate) so the sentence can say
# "among N annotated in total" rather than silently truncating.
_MAX_PROTEIN_SAMPLE = 5


def _columns_literal(columns: dict[str, str]) -> str:
    """``{'pathway_id': 'VARCHAR', ...}`` -- DuckDB struct-literal syntax for
    ``read_csv(..., columns=...)``. Built from a plain dict so the schema is
    declared once, in Python, not duplicated as raw SQL text."""
    return "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"


def _find_sibling(directory: Path, stem: str) -> Path | None:
    """Best-effort: return the first existing candidate file for ``stem`` in
    ``directory``, or ``None`` if none of its candidate extensions exist.
    Never raises -- a primary file with zero, one, or both siblings present
    are all normal, supported cases (mirrors pubchem's ``_find_siblings``)."""
    for ext in _CANDIDATE_EXTS:
        candidate = directory / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _build_query(primary_path: Path, summation_path: Path | None, protein_path: Path | None) -> str:
    """Build the DuckDB SQL for one primary file's full run: LEFT JOIN a
    deduped ``pathway2summation`` read (QUALIFY-based, see module docstring)
    and a pre-aggregated ``UniProt2Reactome`` CTE (DISTINCT-pair + capped
    ``ROW_NUMBER`` sample + separate total count, see module docstring) onto
    the primary ``ReactomePathways`` read. Either or both siblings absent
    contributes a literal ``NULL``/``0`` column instead of a join -- the row
    shape (``pathway_id``, ``name``, ``species``, ``summation``,
    ``proteins_sample``, ``n_proteins``) is always the same six columns
    regardless of which siblings are present.

    Paths are embedded as SQL string literals (escaped via ``str.replace``
    for a literal single-quote, which Reactome/Windows paths never contain
    in practice) rather than passed as ``?`` params -- DuckDB's CTE-based
    query here has no natural single param list ordering across the
    conditionally-included CTEs, unlike pubchem's flat join list."""

    def _lit(p: Path) -> str:
        return "'" + str(p).replace("'", "''") + "'"

    primary_literal = _columns_literal(_PRIMARY_COLUMNS)
    ctes: list[str] = []

    if summation_path is not None:
        summation_literal = _columns_literal(_SUMMATION_COLUMNS)
        ctes.append(
            "summation_dedup AS ("
            "SELECT pathway_id, summation FROM read_csv("
            f"{_lit(summation_path)}, delim='\\t', header=true, quote='', "
            f"ignore_errors=true, columns={summation_literal}) "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY pathway_id ORDER BY summation) = 1"
            ")"
        )
        summation_select = "s.summation AS summation"
        summation_join = "LEFT JOIN summation_dedup s ON s.pathway_id = p.pathway_id"
    else:
        summation_select = "CAST(NULL AS VARCHAR) AS summation"
        summation_join = ""

    if protein_path is not None:
        protein_literal = _columns_literal(_PROTEIN_COLUMNS)
        ctes.append(
            "protein_pairs AS ("
            "SELECT DISTINCT uniprot_id, pathway_id FROM read_csv("
            f"{_lit(protein_path)}, delim='\\t', header=false, "
            f"columns={protein_literal})"
            ")"
        )
        ctes.append(
            "protein_ranked AS ("
            "SELECT pathway_id, uniprot_id, "
            "ROW_NUMBER() OVER (PARTITION BY pathway_id ORDER BY uniprot_id) AS rn "
            "FROM protein_pairs"
            ")"
        )
        ctes.append(
            "protein_agg AS ("
            "SELECT pp.pathway_id AS pathway_id, "
            "STRING_AGG(r.uniprot_id, ', ' ORDER BY r.uniprot_id) AS proteins_sample, "
            "COUNT(*) AS n_proteins "
            "FROM protein_pairs pp "
            "JOIN protein_ranked r ON r.pathway_id = pp.pathway_id "
            f"AND r.uniprot_id = pp.uniprot_id AND r.rn <= {_MAX_PROTEIN_SAMPLE} "
            "GROUP BY pp.pathway_id"
            ")"
        )
        protein_select = (
            "pa.proteins_sample AS proteins_sample, " "COALESCE(cnt.n_proteins, 0) AS n_proteins"
        )
        protein_join = (
            "LEFT JOIN protein_agg pa ON pa.pathway_id = p.pathway_id "
            "LEFT JOIN (SELECT pathway_id, COUNT(*) AS n_proteins "
            "FROM protein_pairs GROUP BY pathway_id) cnt "
            "ON cnt.pathway_id = p.pathway_id"
        )
    else:
        protein_select = "CAST(NULL AS VARCHAR) AS proteins_sample, 0 AS n_proteins"
        protein_join = ""

    with_clause = ("WITH " + ", ".join(ctes) + " ") if ctes else ""
    sql = (
        f"{with_clause}"
        "SELECT p.pathway_id AS pathway_id, p.name AS name, p.species AS species, "
        f"{summation_select}, {protein_select} "
        f"FROM read_csv({_lit(primary_path)}, delim='\\t', header=false, "
        f"columns={primary_literal}) p "
        f"{summation_join} {protein_join} "
        "ORDER BY p.pathway_id"
    )
    return sql


def _build_text(rec: dict[str, Any]) -> str:
    """Template-based declarative sentence for one pathway (SP4 task-9
    brief's own "combining pathway description with ... gene/protein-
    membership prose" framing). No LLM rewriting -- every clause is a plain
    field substitution or omitted when the source/sibling field is absent.

    See module docstring's MIN_OK_TEXT_LEN section for the real character-
    count findings this template produces against the fixture rows.
    """
    name = rec.get("name")
    species = rec.get("species")
    summation = rec.get("summation")
    proteins_sample = rec.get("proteins_sample")
    n_proteins = rec.get("n_proteins") or 0

    sentence = f"Reactome pathway {name} ({species})"
    if summation:
        sentence += f". {summation}"
    else:
        sentence += "."

    if proteins_sample:
        sample_count = len(proteins_sample.split(", "))
        if n_proteins > sample_count:
            sentence += (
                f" Known protein participants include {proteins_sample}, "
                f"among {n_proteins} annotated in total."
            )
        else:
            sentence += f" Known protein participants include {proteins_sample}."

    sentence += " As catalogued in the Reactome pathway database."
    return sentence


def reactome_row(rec: dict[str, Any], source_file: str) -> dict[str, Any]:
    """One joined pathway record -> a finalized ``episteme.articles`` row."""
    pathway_id = rec.get("pathway_id")
    native_id = str(pathway_id) if pathway_id is not None else None
    lic, lic_url, lic_raw = normalize_license(_REACTOME_LICENSE_RAW)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"{SOURCE}:{native_id}" if native_id else f"{SOURCE}:{source_file}:unknown",
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


def iter_rows_from_file(path: Path, *, source_file: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield one ``episteme.articles`` row per pathway_id in one Reactome
    ``ReactomePathways`` primary file (bare ``.txt``/``.tsv``, or the real
    ``.txt.gz``/bare ``.gz`` download shape -- DuckDB's CSV reader
    transparently decompresses ``.gz`` based on the path, same idiom as
    serialize_pubchem.py/serialize_clinvar.py's ``read_csv`` calls),
    best-effort-enriched from ``pathway2summation`` (deduped) and
    ``UniProt2Reactome`` (pre-aggregated) sibling files if present alongside
    it.

    NOT a streaming read despite the generator shape: ``con.execute(...)`` +
    ``con.fetchall()`` materializes the whole join in memory before the
    first row is yielded (same DuckDB DBAPI limitation as chembl/pubchem/
    clinvar's own ``iter_rows_from_file``). A real full ``ReactomePathways
    .txt`` is ~23.6K rows -- small enough that this is not a practical
    concern for this source (unlike clinvar's ~4.56M-row scale), but flagged
    for consistency with the other structured serializers' own named
    concern.
    """
    source_file = source_file or path.name
    directory = path.parent
    summation_path = _find_sibling(directory, _SUMMATION_STEM)
    protein_path = _find_sibling(directory, _PROTEIN_STEM)
    sql = _build_query(path, summation_path, protein_path)
    con = duckdb.connect()
    try:
        con.execute(sql)
        cols = [d[0] for d in con.description]
        for raw in con.fetchall():
            rec = dict(zip(cols, raw, strict=True))
            yield reactome_row(rec, source_file)
    finally:
        con.close()


def discover_reactome_files(raw_dir: Path) -> list[Path]:
    """Discover primary files by a PREFIX-wildcard pattern
    (``ReactomePathways*.txt`` etc.), not an exact-name match: the real
    download always lands as exactly ``ReactomePathways.txt`` (a real
    ``raw_dir`` from a full ``download_reactome.sh`` run only ever holds
    one), but the wildcard lets a manually-assembled raw_dir (or a unit
    test, mirroring pubchem/uniprot's own multi-file ``--max-files`` test
    idiom) hold several distinctly-named primary files side by side.
    Deliberately NOT a generic ``*.tsv``/``*.txt`` fallback (unlike
    clinvar's single-fixture-file idiom) -- see ``_CANDIDATE_EXTS``'s
    comment for why: this task's fixture dir holds three distinctly-named
    files and a generic pattern would make all three match as independent
    primaries."""
    patterns = [f"{_PRIMARY_STEM}*{ext}" for ext in _CANDIDATE_EXTS]
    files = discover_input_files(raw_dir, patterns)
    files.sort(key=lambda p: input_key(p, raw_dir))
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror serialize_chembl.py / serialize_pubchem.py / serialize_clinvar
    .py's ``_best_effort_audit`` shape verbatim, including the settled
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
                pass


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
    try:
        for row in iter_rows_from_file(path, source_file=basename):
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
    else:
        print(f"FAIL {basename}: {result.get('error')}", file=sys.stderr)


def serialize_reactome(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``ReactomePathways*`` primary files under
    ``raw_dir`` (best-effort enriched from sibling ``pathway2summation``/
    ``UniProt2Reactome`` files in the same directory) into staging shards +
    ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_reactome_files(raw_dir)
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
    files = discover_reactome_files(raw_dir)
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
        prog="python -m episteme.data.reactome.serialize_reactome",
        description="Reactome ReactomePathways.txt (+ pathway2summation/UniProt2Reactome "
        "siblings) to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "reactome")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered ReactomePathways* primary files (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_reactome(
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
