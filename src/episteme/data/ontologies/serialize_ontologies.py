#!/usr/bin/env python3
"""Structured serializer: ontologies -- parse GO/HPO/MONDO's OBO/OWL term
ontologies into the unified ``episteme.articles`` row schema (contract
v1.4), one row per term.

Scope decision (real, documented gap -- not a silent omission): the
``ontologies`` source token covers FOUR independently-licensed vocabularies
per ``download_ontologies.sh`` (SP3): GO, HPO, MONDO (all genuine OBO/OWL
term ontologies -- ``pronto.Ontology(path)`` reads them directly, per the
brief) and UCUM (``ucum-essence.xml``). UCUM is NOT an OBO/OWL ontology --
it is a completely different, bespoke XML schema representing units-of-
measure definitions (base units, prefixes, derived units), not a "term with
a definition" prose shape. ``pronto`` cannot parse it, and a UCUM-specific
XML parser is a different unit-of-work shape entirely, out of this task's
scope (deferred as a follow-up, per the controller's explicit ruling --
would be its own small task if ever prioritized). This module therefore
processes every ``.obo``/``.owl`` file it finds under ``raw_dir`` (matching
the brief's own "processes every .obo/.owl file it finds" instruction,
which already implicitly excludes UCUM's ``.xml`` format) and does not
touch ``ucum-essence.xml``/``README.md`` at all.

Real download layout (confirmed by reading ``download_ontologies.sh``
directly, SP3): unlike mesh's/reactome's flat raw dirs, ``ontologies``
nests each vocabulary in its OWN subdirectory --
``01_raw/ontologies/go/{go.obo,go.owl}``,
``01_raw/ontologies/hpo/{hp.obo,hp.owl}``,
``01_raw/ontologies/mondo/{mondo.obo,mondo.owl}``. Discovery therefore
recurses (``Path.rglob``), unlike the legacy flat-dir-oriented ``list_input_files``
precedent (mesh/reactome's fixtures/real downloads are single-directory).

``.obo``/``.owl`` pair collision (a real structural issue the brief's own
"same shape as Task 5" + "processes every .obo/.owl file it finds" wording
leaves ambiguous, resolved here): ``download_ontologies.sh`` fetches BOTH
``go.obo`` AND ``go.owl`` (same vocabulary, two serializations, IDENTICAL
term CURIEs) into the same subdirectory -- ditto hp.obo/hp.owl,
mondo.obo/mondo.owl. Processing both files verbatim would emit two rows
sharing one ``id`` (``ontologies:GO:0008150`` from both go.obo AND go.owl)
per term -- exactly the cross-``source_file`` id collision class the SP4
plan's own goal line names as needing loader hardening; this module must
not manufacture that. The brief's own "one ``source_file`` per ontology"
framing (Interfaces section) is the resolution: discovery groups candidate
files by their PARENT DIRECTORY (one directory = one vocabulary in the real
layout) and keeps only the ``.obo`` file when both extensions are present
in that directory (``.obo`` is pronto's primary/fastest path, smaller
download, and avoids depending on pronto's weaker RDF/XML OWL reader for a
file the size of ``mondo.owl``) -- falling back to ``.owl`` only if no
``.obo`` sibling exists in that same directory (e.g. a partial/
--max-files-capped real download). This is a deliberate reading of the
brief's internal tension, not a scope cut -- documented here per the
controller's flag-before-silently-expanding-scope discipline (mirrors the
posture task-9's reactome module took for its own multi-file
unit-of-work decision).

Real per-term shape (confirmed against pronto 2.7.3 / fastobo 0.14.1, the
versions actually installed in this repo's venv at implementation time,
2026-09-17, against a real header+3-term slice fetched directly from
``http://purl.obolibrary.org/obo/hp.obo`` -- see
tests/fixtures/sp4/ontologies/sample.obo and this task's test module
docstring for the exact real bytes and character-count findings):
``pronto.Ontology(path)`` exposes ``onto.terms()`` (unordered -- sorted by
``term.id`` here for determinism); each ``Term`` has ``.id`` (the CURIE,
e.g. ``HP:0000001``/``GO:0008150`` -- pronto does NOT further-namespace it,
this IS the native id, matches the brief's pinned
``id=f"ontologies:{term.id}"`` exactly, no re-prefixing), ``.name`` (str,
always observed present in practice; ``term.name or native_id`` is this
module's defensive fallback for the theoretical case it is not), and
``.definition`` (a ``pronto.definition.Definition`` object whose ``str()``
gives the definition prose, or ``None`` when the term carries no ``def:``
line at all -- confirmed real and structurally optional, e.g. the fixture's
own ``HP:0000001`` "All" root term).

``text`` template (brief's own pinned shape, literal, NOT extended): "
``f"{name}: {definition}"``" when a definition is present, else just
``name``. Deliberately NOT padded with a trailing boilerplate sentence the
way mesh/reactome's own templates are (their MIN_OK_TEXT_LEN gate math
depends on that padding) -- the brief pins this template exactly, and
padding it to game the gate would misrepresent the real character-count
finding below.

MIN_OK_TEXT_LEN (200 chars) -- checked against real fixture rows, THREE
data points (not just a well-defined/sparse pair, because the third one
surfaces a genuine finding): a real HPO term WITH a real, non-empty
definition (``HP:0000002`` "Abnormality of body height", def 107 chars,
combined text 135 chars) still lands ``extract_status="partial"`` -- real
HPO/GO/MONDO definitions are frequently short one-sentence glosses, not
paragraphs, and this brief's unpadded template does not reliably clear 200
chars even when a genuine definition is present. Only richer definitions
(``HP:0000003`` "Multicystic kidney dysplasia", 290 combined chars) clear
the gate -> ``"ok"``. A name-only term (``HP:0000001`` "All", no ``def:``
line) lands ``"partial"`` at 3 chars. Never ``"empty"``: every real
``[Term]`` stanza carries a non-empty ``name:`` line, and this module's
``term.name or native_id`` fallback guarantees a non-empty title even in
the theoretical case it does not. This is an honest finding, matching every
prior SP4 structured source's own near-boundary posture for sparse/short
real records -- not a bug, and not something this module papers over with
added boilerplate.

Licence: each of the four vocabularies has its OWN declared licence,
extracted PER-FILE from the real OBO header's ``property_value: <curie or
IRI ending in "license"> <value>`` line (``onto.metadata.annotations`` --
confirmed against pronto 2.7.3's real API shape: a ``ResourcePropertyValue``
(bare-URL value, ``.resource``) or ``LiteralPropertyValue`` (quoted-string
value, ``.literal``), matched by property key suffix, not a fixed CURIE
string, since GO/MONDO/HPO all use the ``terms:license`` prefix but a
differently-configured OBO file could use the full
``http://purl.org/dc/terms/license`` IRI instead -- confirmed both forms
parse to the same suffix match in a standalone probe at implementation
time). Real per-vocabulary findings, each run through the ordinary
``article_schema.normalize_license()``/``subset_from_license()`` machinery
(NOT a hardcode), confirmed by fetching each file's real header directly at
implementation time (2026-09-17, byte-range HTTP GETs against
``http://purl.obolibrary.org/obo/{go,hp,mondo}.obo``) and running the exact
declared string through ``normalize_license`` directly, not guessed:

  * GO:    ``property_value: terms:license http://creativecommons.org/licenses/by/4.0/``
    -> ``normalize_license`` -> ``license="unknown"`` -> ``open_metadata``.
    FLAGGED: the bare CC-BY URL contains neither a literal "CC BY"/"CC-BY"
    token nor the phrase "creative commons attribution" that
    ``normalize_license``'s existing CC-BY regex arm requires -- confirmed
    by running this exact real string through the function directly. A
    real, genuine licence-resolution gap (this ontology IS openly
    CC-BY-4.0-licensed in fact; the schema's regex just does not recognise
    a bare CC URL lacking the textual tag), same posture as pubchem/
    clinvar/mesh's own flagged gaps -- NOT patched by adding a new
    ``normalize_license`` arm (spec Sec 8 item 4's own instruction).
  * MONDO: ``property_value: terms:license http://creativecommons.org/licenses/by/4.0/``
    (byte-identical declared string to GO's) -> same resolution, same flag.
  * HPO:   ``property_value: terms:license https://hpo.jax.org/app/license``
    -- a bare project-specific URL, no CC/SPDX token at all -> ``unknown``
    -> ``open_metadata``, exactly as the brief's own instruction to check a
    non-resolving case would predict. This is the vocabulary used for both
    this module's fixture and its real end-to-end run (see "Real
    end-to-end" note below).
  * UCUM:  out of scope this task (format-incompatible, see the scope
    decision above) -- its real licence (UCUM's own README states a
    permissive "no restrictions" grant) is NOT checked or extracted here;
    deferred alongside the parser itself.

``article_schema.py`` is NOT touched by this task's diff (no new
``normalize_license`` arm added) -- all three checked vocabularies'
real declared licences resolve to ``unknown``/``open_metadata`` via the
EXISTING machinery, flagged, not forced.

Ontology terms carry no bibliographic shape beyond a name: ``title`` is
``term.name`` (per the brief -- unlike mesh/reactome, which set ``title``
to ``None`` for their non-bibliographic sources); ``journal``/``year``/
``authors`` are always ``None`` (mirrors every other structured source's
convention). ``container_id``/``book_meta`` are ``None`` on every row. No
hierarchy graph table for ontologies this phase (spec Sec 7 non-goals) --
this module does not build or wire a ``graph_ontologies.sh``, and does not
extend ``graph_builder.py``.

Importable core: ``serialize_ontologies(raw_dir, processed_dir, *,
max_files=0, force=False, workers=1, verbose=False) -> dict``. ``main()``
is the thin CLI wrapper; ``--report`` parses in memory and prints a
field-shape table without writing any shard, marker, manifest or audit row.

Real end-to-end note: HPO (``hp.obo``) was used for the real download ->
serialize proof (10.8MB, the smallest of the three real OBO files --
confirmed via a real HTTP HEAD request at implementation time: GO
36.7MB, HPO 10.8MB, MONDO 53.1MB -- the most practical to fetch in full
within this task's budget). See task-11-report.md for the exact real
end-to-end row counts / extract_status histogram / license breakdown.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5):
  python -m episteme.data.ontologies.serialize_ontologies \\
    --raw-dir ./01_raw/ontologies \\
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

import pronto

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
    input_key,
    is_success,
    mark_failed,
    mark_success,
    write_run_manifest,
)
from episteme.data.staging_writer import write_rows  # noqa: E402

SOURCE = "ontologies"

# Real per-vocabulary OBO/OWL extensions this module processes. UCUM's
# ucum-essence.xml is deliberately excluded -- see module docstring's scope
# decision. Order matters: ".obo" is preferred over ".owl" within the same
# directory (see discover_ontology_files).
_OBO_EXT = ".obo"
_OWL_EXT = ".owl"

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors serialize_mesh.py / serialize_reactome.py / etc.
_AUDIT_LOCK = threading.Lock()


def discover_ontology_files(raw_dir: Path) -> list[Path]:
    """Discover one file per vocabulary directory under ``raw_dir``,
    recursing into subdirectories (the real ``download_ontologies.sh``
    layout nests each vocabulary in its own subdir -- unlike mesh's/
    reactome's flat raw dirs). Candidates are grouped by PARENT DIRECTORY;
    within a directory, ``.obo`` files are preferred and ``.owl`` siblings
    are skipped entirely (see module docstring's ".obo/.owl pair collision"
    section for why -- prevents a cross-source_file id collision on
    identical term CURIEs). A directory with only ``.owl`` file(s) still
    surfaces them (partial/capped real download).

    A flat directory (this module's own fixture dir, or a hand-assembled
    multi-vocabulary test dir with no subdirectories) is just one more
    "directory group" under this same logic -- distinctly-named files in
    the SAME flat directory are NOT deduplicated against each other (only
    the .obo-over-.owl EXTENSION preference within one directory applies).
    """
    raw_dir = Path(raw_dir)
    if not raw_dir.is_dir():
        return []

    by_dir: dict[Path, list[Path]] = {}
    for ext in (_OBO_EXT, _OWL_EXT):
        for p in raw_dir.rglob(f"*{ext}"):
            if p.is_file():
                by_dir.setdefault(p.parent, []).append(p)

    files: list[Path] = []
    for _directory, candidates in by_dir.items():
        obo = sorted(p for p in candidates if p.suffix == _OBO_EXT)
        owl = sorted(p for p in candidates if p.suffix == _OWL_EXT)
        files.extend(obo if obo else owl)

    files.sort(key=lambda p: input_key(p, raw_dir))
    return files


def _property_key(name: str) -> str:
    """Reduce a property_value's ``property`` field (a prefixed CURIE like
    ``terms:license``, or occasionally a full IRI like
    ``http://purl.org/dc/terms/license``) to its final path/CURIE segment,
    lower-cased -- so both forms match ``"license"`` uniformly regardless of
    which the source file used. Confirmed both real forms reduce identically
    in a standalone pronto probe at implementation time."""
    return name.rsplit("/", 1)[-1].rsplit(":", 1)[-1].strip().lower()


def _extract_license_raw(onto: pronto.Ontology) -> str | None:
    """The ontology's own declared licence string, read from its real OBO
    header (``onto.metadata.annotations`` -- a set of ``property_value``
    entries; see module docstring's "Licence" section for the confirmed
    ``ResourcePropertyValue``/``LiteralPropertyValue`` shape). ``None`` if no
    ``property_value`` with a ``license``-suffixed property key is present
    (not observed missing in any of the three real GO/HPO/MONDO headers
    checked at implementation time, but not guaranteed by the OBO format --
    ``normalize_license(None)`` already resolves this gracefully to
    unknown/open_metadata). Sorted before picking the first match only to
    keep the (never actually observed) multiple-match case deterministic."""
    matches: list[str] = []
    for pv in getattr(onto.metadata, "annotations", ()):
        prop = str(getattr(pv, "property", "") or "")
        if _property_key(prop) != "license":
            continue
        value = getattr(pv, "resource", None)
        if value is None:
            value = getattr(pv, "literal", None)
        if value:
            matches.append(str(value))
    if not matches:
        return None
    return sorted(matches)[0]


def _build_text(name: str, definition: str | None) -> str:
    """Brief's own pinned template, literal: ``f"{name}: {definition}"``
    when a definition is present, else just ``name``. Deliberately NOT
    padded with boilerplate -- see module docstring's MIN_OK_TEXT_LEN
    section for why an unpadded template is the honest choice here."""
    if definition:
        return f"{name}: {definition}"
    return name


def ontology_row(term: Any, source_file: str, license_raw: str | None) -> dict[str, Any]:
    """One parsed ``pronto`` ``Term`` -> a finalized ``episteme.articles``
    row. ``id=f"ontologies:{term.id}"`` -- ``term.id`` IS the term's own
    CURIE (e.g. ``GO:0008150``/``HP:0000001``), used verbatim, never
    re-prefixed or transformed further (per the brief's explicit
    instruction)."""
    native_id = str(term.id)
    name = term.name or native_id
    definition = str(term.definition) if term.definition else None

    lic, lic_url, lic_raw = normalize_license(license_raw)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"{SOURCE}:{native_id}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": native_id,
        "pmid": None,
        "pmcid": None,
        "doi": None,
        "title": name,
        "abstract": None,
        "body_text": None,
        "text": _build_text(name, definition),
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
    """Yield one ``episteme.articles`` row per term in one OBO/OWL ontology
    file. ``pronto.Ontology(path)`` reads the whole file (not a streaming
    parse -- real GO/MONDO files run tens of MB with tens of thousands of
    terms; small enough in practice for pronto/fastobo's native parser at
    this source's real scale, unlike mesh's ~314MB single-document XML case
    that specifically required ``iterparse`` streaming). Terms are sorted by
    ``term.id`` for deterministic output ordering (``onto.terms()`` is
    unordered)."""
    source_file = source_file or path.name
    onto = pronto.Ontology(str(path))
    license_raw = _extract_license_raw(onto)
    for term in sorted(onto.terms(), key=lambda t: str(t.id)):
        yield ontology_row(term, source_file, license_raw)


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror serialize_mesh.py / serialize_reactome.py's
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


def serialize_ontologies(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse one OBO/OWL file per vocabulary directory
    under ``raw_dir`` into staging shards + ops markers under
    ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_ontology_files(raw_dir)
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
    files = discover_ontology_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no .obo/.owl ontology files under {raw_dir}", file=sys.stderr)
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
        prog="python -m episteme.data.ontologies.serialize_ontologies",
        description="GO/HPO/MONDO OBO/OWL ontology files to episteme.articles staging shards "
        "(UCUM excluded -- see module docstring)",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "ontologies")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered ontology files (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_ontologies(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no .obo/.owl ontology files under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
