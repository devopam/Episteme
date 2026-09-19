#!/usr/bin/env python3
"""Structured serializer: ClinVar -- parse a downloaded ClinVar
``variant_summary.txt.gz`` (NCBI's ``tab_delimited`` release, the default
mode of ``download_clinvar.sh``) into the unified ``episteme.articles`` row
schema (contract v1.4).

Unit-of-work: SIMPLER than pubchem's task-7 (no multi-file LEFT JOIN) --
confirmed against a real download, not assumed. ``download_clinvar.sh``'s
default ``tsv`` mode fetches ClinVar's whole ``tab_delimited/`` directory
(``allele_gene.txt.gz``, ``cross_references.txt``, ``hgvs4variation.txt.gz``,
``submission_summary.txt.gz``, ``var_citations.txt``,
``summary_of_conflicting_interpretations.txt`` (1.8 GB uncompressed --
genuinely impractical to fetch for this task), ``variation_allele.txt.gz``,
etc.) -- but ``variant_summary.txt.gz`` alone is single-file, self-contained,
and carries every column this row shape needs (AlleleID/Type/Name/GeneID/
GeneSymbol/ClinicalSignificance/PhenotypeList/RCVaccession/ReviewStatus/
Assembly/VariationID and more). No sibling-file join is attempted or needed
-- this module's discovery patterns match ONLY ``variant_summary*`` (plus a
generic ``*.tsv`` fallback for the hand-built fixture, mirroring chembl's own
generic ``*.db`` fixture-discovery idiom), not the other files in that
directory.

Real-file column-name confirmation (2026-09-16, against a full real download
-- ``scripts/data/run_pipeline.sh clinvar download --max-files 1`` for the
pipeline-wiring proof, then a direct full fetch of
``tab_delimited/variant_summary.txt.gz``, 443,063,763 bytes, matching the
server's own ``Content-Length`` exactly): the real header line is
    #AlleleID\tType\tName\tGeneID\tGeneSymbol\tHGNC_ID\tClinicalSignificance\t
    ClinSigSimple\tLastEvaluated\tRS# (dbSNP)\tnsv/esv (dbVar)\tRCVaccession\t
    PhenotypeIDS\tPhenotypeList\tOrigin\tOriginSimple\tAssembly\t
    ChromosomeAccession\tChromosome\tStart\tStop\tReferenceAllele\t
    AlternateAllele\tCytogenetic\tReviewStatus\tNumberSubmitters\tGuidelines\t
    TestedInGTR\tOtherIDs\tSubmitterCategories\tVariationID\tPositionVCF\t
    ReferenceAlleleVCF\tAlternateAlleleVCF\tSomaticClinicalImpact\t...
Two deviations from the ``tab_delimited/README``'s documented column list,
both confirmed against the real bytes, not the (stale) prose:
  1. The first column literally starts with ``#`` (``#AlleleID``), not
     ``AlleleID``.
  2. The phenotype-identifiers column is spelled ``PhenotypeIDS`` (capital
     S) in the real header, not ``PhenotypeIDs`` as the README's prose has
     it. (Not used by this row shape -- ``PhenotypeList``, the
     human-readable sibling column, is used instead -- but the exact real
     spelling is recorded here in case a future task needs it.)
Neither column is referenced by this module's SQL (no special-character
quoting needed for the columns actually selected), but both are recorded
here as the concrete evidence this task's brief asked for: "CONFIRM the
exact real column names/casing against the actual downloaded file rather
than assuming."

Primary key choice: ``VariationID`` ("The identifier ClinVar uses specific
to the AlleleID" -- tab_delimited/README's own words), confirmed
all-integer across the full real file (9,056,310 data rows, zero non-numeric
values found) -- NOT ``RCVaccession`` (a pipe-separated LIST of accessions
per row, not a scalar id) and NOT ``AlleleID`` (a sub-variant allele
identifier, not ClinVar's own "variant record" identifier per the brief's
framing).

Assembly-duplicate dedup (the one real wrinkle this task's "simpler than
pubchem" framing didn't spell out, discovered by inspecting the real file):
``variant_summary.txt`` reports each variant ONCE PER GENOME ASSEMBLY it has
coordinates on -- confirmed empirically: 9,056,310 real data rows but only
4,562,367 distinct ``VariationID`` values (~2x), and the real
``Assembly`` column takes exactly 4 values (GRCh37: 4,547,596 rows; GRCh38:
4,494,430; NCBI36: 4,771; ``na``: 9,513). A naive ``id=f"clinvar:{variation_
id}"`` with NO dedup would therefore emit ~2 rows sharing the same id for
most variants in a real full run -- silently violating the row-id-uniqueness
convention every other SP4 serializer's test suite asserts (chembl/pubchem:
``len({row["id"] for row in rows}) == n_rows``). This module dedups IN SQL
(``QUALIFY ROW_NUMBER() OVER (PARTITION BY VariationID ORDER BY <assembly
preference>) = 1``, preferring GRCh38 > GRCh37 > NCBI36 > anything else) so
exactly one row is emitted per distinct VariationID -- matching the brief's
own "one row per variant record" framing literally (a "record" is one
variant, not one variant-assembly pairing).
Verified BEFORE committing to this design that it drops no real TEXT
content: scanned the full real file for every VariationID with >1 physical
row and confirmed Name / ClinicalSignificance / PhenotypeIDs / PhenotypeList
are byte-identical across a variant's assembly-duplicate rows in EVERY case
(0 mismatches across all ~4.5M duplicated VariationIDs) -- only genomic
coordinate columns (Chromosome/Start/Stop/PositionVCF/ReferenceAllele/
AlternateAllele/ChromosomeAccession), which this row shape does not carry
into ``text`` at all, differ between a variant's assembly rows. Picking
either assembly therefore never changes this module's output text.

ClinVar variant records carry no bibliographic shape: ``title``/``journal``/
``year``/``authors`` are always ``None`` (mirrors chembl/uniprot/pubchem's
convention). ``container_id``/``book_meta`` are ``None`` on every row.

``text`` template (SP4 task-8 brief's own illustrative example, ClinVar's
real ``"-"`` missing-value sentinel normalized to SQL NULL via ``NULLIF`` so
an absent ``ClinicalSignificance`` -- real, observed: VariationID 4507303 in
this module's own fixture -- omits that clause entirely instead of rendering
the literal token ``"-"`` or fabricating "unknown"):
  "Variant {name} is classified {clinical_significance} for
  {phenotype_list}, per ClinVar accession {rcv_accession}, as catalogued in
  the NCBI ClinVar database."
MIN_OK_TEXT_LEN (200 chars) sanity-check against real fixture rows (see
task-8-report.md for the exact character counts): a well-populated record
with a long HGVS ``Name`` and a multi-condition ``PhenotypeList`` (real
VariationID 2: 3 pipe-separated RCVs, 3 phenotype entries incl. one literal
``"not provided"``) clears 200 chars comfortably -> ``extract_status="ok"``.
A record whose ENTIRE ``PhenotypeList`` is the single real value
``"not provided"`` (VariationID 487086 -- exactly the brief's named "empty
phenotype" case) has a much shorter sentence and lands under the threshold
-> ``extract_status="partial"`` -- honest, not a bug, mirroring every prior
SP4 structured source's near-boundary finding for compact records.

Licence: ClinVar's own real "Disclaimer and data use policy" text (fetched
directly from https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/ at
implementation time, 2026-09-16 -- there is no dedicated LICENSE file under
the ``tab_delimited/`` FTP directory or the ClinVar FTP root; this policy
page is ClinVar's own authoritative statement of terms). It is NOT a
CC-variant string -- an NIH/NCBI disclaimer plus an attribution REQUEST
("we ask that you provide attribution"), not a copyright grant or
requirement. Run through the ordinary
``article_schema.normalize_license``/``subset_from_license`` machinery (NOT
a hardcode) -- confirmed it falls through every existing arm (no CC/BY/NC/
SA/ND token, no "TEXT MINING"/"FAIR USE" substring, no permissive-OSI
identifier match) to ``license="unknown"`` ->
``subset_from_license("unknown")`` == ``"open_metadata"`` (the conservative
default). EXPECTED and ACCEPTABLE per this task's brief (same posture as
pubchem task-7's spec Sec 8 open item 2) -- flagged here and in
task-8-report.md, NO new ``normalize_license`` arm added, ``article_schema.py``
is NOT touched by this task's diff.

Importable core: ``serialize_clinvar(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table without
writing any shard, marker, manifest or audit row.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5):
  python -m episteme.data.clinvar.serialize_clinvar \\
    --raw-dir ./01_raw/clinvar \\
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
    is_success,
    list_input_files,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "clinvar"

# ClinVar's own real "Disclaimer and data use policy" text, fetched directly
# from https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/ at
# implementation time (2026-09-16) -- NOT a CC-variant string. Run through
# normalize_license()/subset_from_license() like every other field -- NOT a
# hardcode. See module docstring's "Licence" section for the confirmed
# unknown -> open_metadata resolution and the explicit flag (no new
# normalize_license arm added).
_CLINVAR_LICENSE_RAW = (
    "The information on this website is not intended for direct diagnostic "
    "use or medical decision-making without review by a genetics "
    "professional. Individuals should not change their health behavior "
    "solely on the basis of information contained on this website. NIH "
    "does not independently verify the submitted information. If you have "
    "questions about the information contained on this website, please "
    "see a health care professional. More information about NCBI's "
    "disclaimer policy is available. If you distribute or copy data from "
    "ClinVar, we ask that you provide attribution to ClinVar as a data "
    "source in publications and websites. You can cite one of the "
    "ClinVar publications (e.g. PMID: 29165669)."
)

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors extract_pubmed.py / serialize_chembl.py / serialize_pubchem.py.
_AUDIT_LOCK = threading.Lock()

# Primary file discovery: the real download_clinvar.sh (tsv/default mode)
# artifact is always exactly "variant_summary.txt.gz" under a
# "tab_delimited/" subdirectory of raw_dir -- list_input_files's rglob
# covers the nesting. The trailing "*.tsv" pattern is a generic fixture-only
# fallback (mirrors chembl's own generic "*.db" fixture-discovery idiom) so
# a hand-built/extracted fixture doesn't need to be named "variant_summary*"
# -- this task's own fixture is "sample.tsv".
_PRIMARY_STEM = "variant_summary"
_CANDIDATE_EXTS = (".txt.gz", ".gz", ".tsv", ".txt")

# One row per distinct VariationID (see module docstring's "Assembly-
# duplicate dedup" section for the full empirical justification). ClinVar's
# real "-" missing-value sentinel is normalized to SQL NULL via NULLIF for
# every field this module reads, so a genuinely absent value (e.g.
# ClinicalSignificance="-", observed for real VariationID 4507303) omits its
# clause in _build_text instead of rendering the literal "-" token.
#
# Only the columns ``_build_text``/``clinvar_row`` actually read are
# selected -- ``Type``/``GeneSymbol``/``ReviewStatus`` are NOT (an earlier
# draft selected them speculatively; dropped after a self-review pass
# confirmed neither function reads them). This matters beyond tidiness: at
# real full-file scale (~4.56M deduped rows), every unused column is a few
# hundred MB of Python strings ``con.fetchall()`` materializes for nothing,
# directly adding to the memory ceiling this task's real end-to-end attempt
# hit (see task-8-report.md's Concern 1). ``assembly`` IS kept, despite not
# feeding ``text`` either, because it is the one column that makes the
# QUALIFY clause's GRCh38-preference actually observable/testable (see
# ``test_clinvar_assembly_dedup_prefers_grch38``) -- without it, which
# assembly row survives the dedup would be a silently untested behaviour.
_QUERY = """
    SELECT
        VariationID                        AS variation_id,
        NULLIF(Name, '-')                  AS name,
        NULLIF(ClinicalSignificance, '-')  AS clinical_significance,
        NULLIF(PhenotypeList, '-')         AS phenotype_list,
        NULLIF(RCVaccession, '-')          AS rcv_accession,
        Assembly                           AS assembly
    FROM read_csv(?, delim='\t', header=true, quote='')
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY VariationID
        ORDER BY CASE Assembly
            WHEN 'GRCh38' THEN 0
            WHEN 'GRCh37' THEN 1
            WHEN 'NCBI36' THEN 2
            ELSE 3
        END
    ) = 1
    ORDER BY VariationID
"""


def _build_text(rec: dict[str, Any]) -> str:
    """Template-based declarative sentence for one deduped VariationID
    record (SP4 task-8 brief's own illustrative example). No LLM rewriting
    -- every clause is a plain field substitution or omitted when the
    source field is absent (ClinVar's real "-" sentinel, normalized to NULL
    by the SQL query's NULLIF calls -- see ``_QUERY``).

    A literal real PhenotypeList value of ``"not provided"`` is NOT treated
    as absent -- it is ClinVar's own genuine submitted value (distinct from
    the "-" missing-value sentinel), so it is kept verbatim in the sentence,
    same as any other phenotype name.
    """
    variation_id = rec.get("variation_id")
    name = rec.get("name")
    clinsig = rec.get("clinical_significance")
    phenotype = rec.get("phenotype_list")
    rcv = rec.get("rcv_accession")

    subject = f"Variant {name}" if name else f"ClinVar variation {variation_id}"

    sentence = subject
    if clinsig:
        sentence += f" is classified {clinsig}"
    if phenotype:
        sentence += f" for {phenotype}"
    if rcv:
        sentence += f", per ClinVar accession {rcv}"
    sentence += ", as catalogued in the NCBI ClinVar database."
    return sentence


def clinvar_row(rec: dict[str, Any], source_file: str) -> dict[str, Any]:
    """One deduped ``VariationID`` record -> a finalized ``episteme.articles`` row."""
    variation_id = rec.get("variation_id")
    native_id = str(variation_id) if variation_id is not None else None
    lic, lic_url, lic_raw = normalize_license(_CLINVAR_LICENSE_RAW)
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


def iter_rows_from_file(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one ``episteme.articles`` row per DISTINCT ``VariationID`` in
    one ClinVar ``variant_summary`` file (bare ``.tsv``/``.txt``, or the
    real ``.txt.gz``/bare ``.gz`` download shape -- DuckDB's CSV reader
    transparently decompresses ``.gz`` based on the path, no separate
    ``gzip.open`` pre-decompress step needed, same idiom as
    ``serialize_pubchem.py``'s ``read_csv`` calls). Assembly-duplicate rows
    (the same VariationID reported once per genome assembly) are deduped in
    SQL -- see module docstring's "Assembly-duplicate dedup" section.

    NOT a streaming read despite the generator shape: ``con.execute(...)`` +
    ``con.fetchall()`` materializes the whole (deduped) result in memory
    before the first row is yielded (same DuckDB DBAPI limitation as
    chembl/pubchem's own ``iter_rows_from_file``). A real full
    ``variant_summary.txt.gz`` is ~9.06M physical rows / ~4.56M distinct
    VariationIDs after dedup -- flagged as the same class of named ops
    concern chembl's task-5 report raised for its own 10^7-row
    ``activities`` table, not fixed here.
    """
    source_file = path.name
    con = duckdb.connect()
    try:
        con.execute(_QUERY, [str(path)])
        cols = [d[0] for d in con.description]
        for raw in con.fetchall():
            rec = dict(zip(cols, raw, strict=True))
            yield clinvar_row(rec, source_file)
    finally:
        con.close()


def discover_clinvar_files(raw_dir: Path) -> list[Path]:
    """Discover primary files: ``variant_summary*`` (the real download
    shape, any of ``_CANDIDATE_EXTS``, found under ``raw_dir`` or any
    subdirectory -- the real ``download_clinvar.sh`` artifact lands at
    ``raw_dir/tab_delimited/variant_summary.txt.gz``) plus a generic
    ``*.tsv`` fallback for this task's own genericaly-named
    ``sample.tsv`` fixture (mirrors chembl's own generic ``*.db``
    fixture-discovery idiom)."""
    patterns = [f"{_PRIMARY_STEM}*{ext}" for ext in _CANDIDATE_EXTS] + ["*.tsv"]
    files = list_input_files(raw_dir, patterns)
    files.sort(key=lambda p: p.name)
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror serialize_chembl.py / serialize_pubchem.py's
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
                pass


def process_one(
    path: Path,
    *,
    processed_dir: Path,
    force: bool,
) -> dict[str, Any]:
    basename = path.name
    if not force and is_success(processed_dir, SOURCE, basename):
        return {"source_file": basename, "skipped": True, "reason": "success_marker"}

    t0 = time.time()
    status_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    try:
        for row in iter_rows_from_file(path):
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


def serialize_clinvar(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse ``variant_summary*``/``*.tsv`` files under
    ``raw_dir`` into staging shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_clinvar_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]

    workers = max(1, int(workers))
    results: list[dict[str, Any]] = []
    t0 = time.time()

    if workers == 1:
        for fp in files:
            result = process_one(fp, processed_dir=processed_dir, force=force)
            results.append(result)
            if verbose:
                _print_verbose(result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(process_one, fp, processed_dir=processed_dir, force=force): fp
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
    files = discover_clinvar_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no {_PRIMARY_STEM}*/*.tsv files under {raw_dir}", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for fp in files:
        for row in iter_rows_from_file(fp):
            rows.append(row)
    print(_render_field_shape(rows, len(files)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        prog="python -m episteme.data.clinvar.serialize_clinvar",
        description="ClinVar variant_summary.txt.gz to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "clinvar")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered variant_summary* primary files (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_clinvar(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no {_PRIMARY_STEM}*/*.tsv files under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
