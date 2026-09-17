#!/usr/bin/env python3
"""Structured serializer: MeSH -- parse NLM's MeSH descriptor XML release
into the unified ``episteme.articles`` row schema (contract v1.4). This is
also the SP4 task that makes ``graph_builder``'s Task 4 ``mesh_hierarchy``
derivation exercisable end-to-end against REAL data for the first time (see
task-10-report.md for the full proof: real download, real serialize, real
load, real graph build, real ``neighbours()`` query).

Real file shape (confirmed 2026-09-17 by fetching
https://nlmpubs.nlm.nih.gov/projects/mesh/2025/xmlmesh/desc2025.xml directly):
one ``<DescriptorRecordSet>`` document holding the ENTIRE MeSH vocabulary --
314,015,481 bytes / ~30K ``<DescriptorRecord>`` elements in ONE file (the
compressed sibling ``desc2025.gz`` is 16,840,289 bytes). ``download_mesh.sh``
(SP3) resolved ZERO files in a live, ``episteme_test``-scoped run against
this real layout: its candidate directories (``$MESH_BASE/xmlmesh$YEAR``,
``$MESH_BASE/ascii$YEAR``, ...) no longer exist -- NLM's current real layout
is ``$MESH_BASE/<year>/xmlmesh/desc<year>.xml``. This is a pre-existing SP3
gap, out of this task's scope (the brief explicitly says ``download_mesh.sh``
"already exists and works -- you do not need to modify it"; empirically, on
2026-09-17, it does not resolve real files, but fixing its directory-listing
logic is not this task's job). This task's real end-to-end (task-10-report.md)
fetched the real ``desc2025.gz`` directly instead, decompressed it, and ran
the full ``serialize -> load -> graph`` chain against the real, complete
2025 MeSH descriptor release.

Real per-descriptor shape (confirmed against the real 2025 file):
``<DescriptorRecord><DescriptorUI>D000001</DescriptorUI><DescriptorName>
<String>Calcimycin</String></DescriptorName>...<ConceptList><Concept
PreferredConceptYN="Y">...<ScopeNote>prose</ScopeNote>...</Concept>
...</ConceptList>...</DescriptorRecord>``. One row per ``DescriptorRecord``:
``id=f"mesh:{descriptor_ui}"``, ``source_record_id=descriptor_ui``,
``title=<DescriptorName><String>``, ``text=<DescriptorName> + the PREFERRED
Concept's <ScopeNote> (if present) -> prose``. A descriptor can carry
multiple ``<Concept>`` elements, each with its own optional ``ScopeNote`` --
this module takes only the one marked ``PreferredConceptYN="Y"``, falling
back to the first ScopeNote seen under any Concept if none is marked
preferred (defensive; not observed missing in the real file).

This module intentionally does NOT persist ``TreeNumberList`` into the row:
``graph_builder``'s Task 4 ``mesh_hierarchy`` derivation re-parses the same
raw descriptor XML independently for parent/child tree-number edges (the
design ruling settled in Task 4, re-confirmed here, not re-litigated) --
this module's own responsibility stays scoped to ``articles`` rows only.

MeSH descriptor records carry no bibliographic shape: ``title`` doubles as
the descriptor name; ``journal``/``year``/``authors`` are always ``None``
(mirrors reactome/pubchem/clinvar/uniprot's convention).
``container_id``/``book_meta`` are ``None`` on every row.

MIN_OK_TEXT_LEN (200 chars) -- checked against BOTH sides per the brief's
own instruction, not assumed:
  * A descriptor WITH a real, well-annotated ScopeNote (e.g. the real
    D000001 "Calcimycin" record's ~400-char preferred ScopeNote, or this
    module's own fixture's D003924 record, which carries the real 2025
    file's actual ~400-char "Diabetes Mellitus, Type 2" ScopeNote prose --
    deliberately attached to the D003924 record for this fixture, a flagged
    mismatch, see tests/data/test_serialize_mesh.py's module docstring)
    clears 200 chars comfortably -> ``extract_status="ok"``.
  * A descriptor with NO ScopeNote at all (structurally optional -- a real,
    observed case, e.g. this module's own fixture's D003920 record) lands
    on the bare "{name}. As defined in the NLM Medical Subject Headings
    (MeSH) thesaurus." sentence alone, well under 200 chars ->
    ``extract_status="partial"`` -- honest, not a bug, same near-boundary
    finding every prior SP4 structured source reports for sparse records.
    Never "empty": every ``DescriptorRecord`` always carries a non-empty
    ``DescriptorName``. See task-10-report.md for the exact real-file
    "ok"/"partial" split.

Fixture note (tests/fixtures/sp4/mesh/sample.xml): reuses Task 4's exact
merged synthetic-fixture SHAPE and content (``test_graph_builder.py::
test_build_populates_mesh_hierarchy`` -- same D003924/D003920 UIs, names and
tree numbers, unmodified), extended with a real
``ConceptList/Concept[@PreferredConceptYN='Y']/ScopeNote`` on D003924 only
(D003920 stays bare) so this module's own unit test exercises both sides of
MIN_OK_TEXT_LEN from one file. The added ScopeNote text is REAL prose,
copied verbatim from the real 2025 release -- but flagged here rather than
silently "fixed": in the real 2025 file the UI/name/tree-number pairing is
actually the OPPOSITE of Task 4's synthetic fixture (real D003920 =
"Diabetes Mellitus" / C18.452.394.750, real D003924 = "Diabetes Mellitus,
Type 2" / C18.452.394.750.149 -- confirmed by parsing the real file
directly). Task 4's fixture swapped this pairing; this task's fixture
preserves that exact (swapped) pairing unchanged, per the brief's explicit
request to reuse Task 4's shape/content for narrative consistency between
the two tests -- the swap versus real NLM data is a pre-existing Task 4
characteristic, not introduced or corrected here.

Streaming: ``defusedxml.ElementTree.iterparse`` (entity-expansion safe --
matches ``jats.py``/``graph_builder.py``'s established security posture, NOT
bare ``xml.etree``), same idiom SP2 Task 12's ``enrich_from_lite.py`` proved
against an 821MB single-XML-document tar member: listen for ``("start",
"end")`` events, grab the root on the first event, on every ``end`` of a
``DescriptorRecord`` extract its fields then ``elem.clear(); root.clear()``
so memory stays flat regardless of file size. Matters here as much as it did
for ``enrich_from_lite.py``: a real full release is 314MB / ~30K descriptors
in ONE document, not a per-file-per-descriptor shape. NOT fully streaming
end-to-end despite this, though: ``process_one`` still accumulates every
parsed row into one Python list before a single ``write_rows`` call per
file (same caveat ``serialize_reactome.py``'s docstring flags for its own
``iter_rows_from_file``) -- acceptable at MeSH's ~30K-descriptor-per-file
scale (small dicts of short strings), unlike, say, clinvar's ~4.56M-row
scale.

``discover_mesh_files`` accepts ``desc*.xml``/``desc*.xml.gz``/``desc*.gz``
(the real download shape) PLUS a generic ``*.xml``/``*.xml.gz`` fallback (so
a differently-named fixture, e.g. this module's own ``sample.xml``, is still
discovered -- mirrors clinvar's own generic-fallback idiom). To keep that
generic fallback from misclassifying MeSH's real SIBLING files that also
ship as ``*.xml`` in the same real download directory -- ``qual<year>.xml``
(QualifierRecordSet), ``supp<year>.xml`` (SupplementalRecordSet),
``pa<year>.xml`` (PharmacologicalActionSet), all confirmed present
alongside ``desc<year>.xml`` in the real 2025 ``xmlmesh/`` listing --
every candidate is cheaply peeked (``_is_descriptor_file``: read only up to
the first ``start`` event) and kept only if its root tag is
``DescriptorRecordSet``. This is a correctness fix, not a naming
heuristic: a name-only "desc*" filter would have been fooled by nothing in
the real listing today, but the peek is the actually-robust mechanism (and
is what lets the differently-named fixture work at all without a special
case).

Licence: NLM's real "Terms and Conditions MeSH" page (fetched directly from
https://www.nlm.nih.gov/databases/download/terms_and_conditions_mesh.html
at implementation time, 2026-09-17; that page's own ``<meta name="DC.Rights"
content="Public Domain">`` tag, and its body text: "NLM freely provides MeSH
data. No charges, usage fees or royalties are paid to NLM for this data."
plus the general terms asking only for source acknowledgement, no
endorsement claims, and version-currency disclosure on redistribution -- no
copyleft, no share-alike, no NC restriction, no literal SPDX/CC token
anywhere in the real text). Run through the ordinary
``article_schema.normalize_license()``/``subset_from_license()`` machinery
(NOT a hardcode) -- confirmed it resolves to ``license="unknown"`` ->
``subset="open_metadata"``, exactly as the brief predicted (a "Public
Domain" declaration with no CC0/CC-BY/permissive-OSI token does not hit any
of ``normalize_license``'s existing regex arms). FLAGGED, not forced: a
genuine gap between MeSH's real free-for-any-use licence and this schema's
license-code vocabulary, same posture as pubchem/clinvar's own flagged
licence gaps -- ``article_schema.py`` is NOT touched by this task's diff.

Importable core: ``serialize_mesh(raw_dir, processed_dir, *, max_files=0,
force=False, workers=1, verbose=False) -> dict``. ``main()`` is the thin CLI
wrapper; ``--report`` parses in memory and prints a field-shape table
without writing any shard, marker, manifest or audit row.

Roadmap CLI (Sec 4.7-shaped, SP4 Sec 5):
  python -m episteme.data.mesh.serialize_mesh \\
    --raw-dir ./01_raw/mesh \\
    --processed-dir ./02_processed \\
    --max-files 20 \\
    [--force]
"""

from __future__ import annotations

import argparse
import gzip
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from defusedxml.ElementTree import iterparse as _safe_iterparse

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
    normalize_whitespace,
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

SOURCE = "mesh"

# NLM's real "Terms and Conditions MeSH" page, fetched directly at
# implementation time (2026-09-17) -- see module docstring's "Licence"
# section for the full confirmed unknown -> open_metadata resolution
# (matches the brief's own prediction, unlike reactome's real-licence match).
_MESH_LICENSE_RAW = (
    "National Library of Medicine (NLM) Terms and Conditions for MeSH data "
    "(page metadata: DC.Rights = Public Domain). NLM freely provides MeSH "
    "data. No charges, usage fees or royalties are paid to NLM for this "
    "data. Users of the data agree to acknowledge NLM as the source of the "
    "data in a clear and conspicuous manner and not indicate or imply that "
    "NLM has endorsed its products/services/applications."
)

# Serializes the mirror_only file-append across this process's worker threads
# (the DB-level advisory lock in audit_trail.record() does nothing for a plain
# file write). Mirrors serialize_chembl.py / serialize_pubchem.py /
# serialize_clinvar.py / serialize_reactome.py.
_AUDIT_LOCK = threading.Lock()

# Candidate discovery patterns. "desc*" matches the real download shape
# (desc<year>.xml / desc<year>.xml.gz / desc<year>.gz); the generic "*.xml"/
# "*.xml.gz" fallback lets a differently-named fixture (this module's own
# sample.xml) be discovered too, same idiom as clinvar's generic "*.tsv"
# fallback -- see module docstring's "discover_mesh_files" section for why a
# ROOT-TAG PEEK (not just this naming pattern) is what actually keeps real
# sibling files (qual*.xml/supp*.xml/pa*.xml) out.
_CANDIDATE_PATTERNS = ["desc*.xml", "desc*.xml.gz", "desc*.gz", "*.xml", "*.xml.gz"]


def _local(tag: str) -> str:
    """Strip any ``{namespace}`` prefix from an ElementTree tag -- real MeSH
    descriptor XML carries no namespace, but this mirrors graph_builder.py's
    defensive idiom for the same file shape at negligible cost."""
    return tag.rsplit("}", 1)[-1] if tag else tag


def _open_source(path: Path):
    """Binary file handle for ``path`` -- transparently gzip-decompressing
    when its suffix is ``.gz`` (covers both the real ``desc<year>.gz`` shape
    and a hypothetical ``desc<year>.xml.gz``; ``Path.suffix`` returns only
    the final extension either way)."""
    return gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")


def _is_descriptor_file(path: Path) -> bool:
    """Cheap root-tag peek: True iff ``path``'s root element is
    ``<DescriptorRecordSet>``. Reads only up to the first ``start`` event
    (negligible cost even against the real 314MB descriptor file) -- see
    module docstring's ``discover_mesh_files`` section for why this, not a
    name-only filter, is what keeps MeSH's real sibling files
    (QualifierRecordSet/SupplementalRecordSet/PharmacologicalActionSet) from
    being misclassified as descriptor primaries. An unreadable/corrupt/
    non-XML candidate is simply not a descriptor file."""
    try:
        with _open_source(path) as fh:
            for _event, elem in _safe_iterparse(fh, events=("start",)):
                return _local(elem.tag) == "DescriptorRecordSet"
    except Exception:  # noqa: BLE001 -- unreadable/corrupt candidate, not a descriptor file
        return False
    return False


def _preferred_scope_note(rec: Any) -> str | None:
    """The preferred Concept's ``<ScopeNote>`` text, or the first ScopeNote
    seen under any Concept if none is marked preferred (defensive; not
    observed missing in the real 2025 file). ``None`` if the descriptor
    carries no ScopeNote at all (structurally optional -- see module
    docstring's MIN_OK_TEXT_LEN section)."""
    preferred = rec.find("ConceptList/Concept[@PreferredConceptYN='Y']/ScopeNote")
    if preferred is not None:
        text = "".join(preferred.itertext()).strip()
        if text:
            return text
    for note in rec.findall("ConceptList/Concept/ScopeNote"):
        text = "".join(note.itertext()).strip()
        if text:
            return text
    return None


def _record_fields(rec: Any) -> tuple[str | None, str | None, str | None]:
    """``(descriptor_ui, descriptor_name, scope_note)`` for one
    ``<DescriptorRecord>`` element (already fully built by iterparse's
    ``end`` event -- ``find``/``findall`` path queries are safe here, no
    further streaming needed for a single record's subtree)."""
    ui_el = rec.find("DescriptorUI")
    ui = (ui_el.text or "").strip() if ui_el is not None and ui_el.text else None
    name_el = rec.find("DescriptorName/String")
    name = (name_el.text or "").strip() if name_el is not None and name_el.text else None
    scope_note = _preferred_scope_note(rec)
    return ui, name, scope_note


def _iter_descriptor_records(fh: Any) -> Iterator[tuple[str, str | None, str | None]]:
    """Stream ``(descriptor_ui, descriptor_name, scope_note)`` tuples out of
    one MeSH descriptor XML file object, one ``<DescriptorRecord>`` at a
    time. Clears the parsed element (and the root's now-stale child
    reference) as it goes so memory stays flat regardless of file size --
    required at MeSH's real scale (a single real release is ~314MB / ~30K
    descriptors in ONE document). Exact call shape copied from
    ``enrich_from_lite.py``'s ``_iter_records`` (SP2 Task 12), the proven
    precedent for this streaming pattern in this repo."""
    context = iter(_safe_iterparse(fh, events=("start", "end")))
    _, root = next(context)
    for event, elem in context:
        if event != "end" or _local(elem.tag) != "DescriptorRecord":
            continue
        ui, name, scope_note = _record_fields(elem)
        if ui:
            yield ui, name, scope_note
        elem.clear()
        root.clear()


def _build_text(name: str | None, scope_note: str | None) -> str:
    """Template-based declarative sentence for one descriptor (brief's own
    "<DescriptorName> + ScopeNote (if present) -> prose" framing). No LLM
    rewriting -- a plain field substitution, omitted when the ScopeNote is
    absent. See module docstring's MIN_OK_TEXT_LEN section for the real
    character-count findings this template produces."""
    sentence = f"{name}." if name else ""
    if scope_note:
        sentence += f" {scope_note}"
    sentence += " As defined in the NLM Medical Subject Headings (MeSH) thesaurus."
    return normalize_whitespace(sentence)


def mesh_row(
    descriptor_ui: str,
    name: str | None,
    scope_note: str | None,
    source_file: str,
) -> dict[str, Any]:
    """One parsed ``<DescriptorRecord>`` -> a finalized ``episteme.articles``
    row."""
    lic, lic_url, lic_raw = normalize_license(_MESH_LICENSE_RAW)
    subset = subset_from_license(lic)

    row: dict[str, Any] = {
        "id": f"{SOURCE}:{descriptor_ui}",
        "source": SOURCE,
        "source_file": source_file,
        "source_record_id": descriptor_ui,
        "pmid": None,
        "pmcid": None,
        "doi": None,
        "title": name,
        "abstract": None,
        "body_text": None,
        "text": _build_text(name, scope_note),
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
    """Yield one ``episteme.articles`` row per ``<DescriptorRecord>`` in one
    MeSH descriptor XML file (bare ``.xml``, or the real ``.gz``/hypothetical
    ``.xml.gz`` compressed shape -- transparently decompressed via
    ``_open_source``). Streams via ``defusedxml.ElementTree.iterparse`` (see
    module docstring); ``source_file`` is ``path.name`` verbatim, matching
    what ``graph_builder.build``'s ``rglob(src_file)`` re-parse lookup
    expects (reactome's precedent)."""
    source_file = path.name
    with _open_source(path) as fh:
        for descriptor_ui, name, scope_note in _iter_descriptor_records(fh):
            yield mesh_row(descriptor_ui, name, scope_note, source_file)


def discover_mesh_files(raw_dir: Path) -> list[Path]:
    """Discover MeSH descriptor files under ``raw_dir`` by name pattern
    (``_CANDIDATE_PATTERNS``) THEN a root-tag peek (``_is_descriptor_file``)
    -- see module docstring for why the peek, not the naming pattern alone,
    is the real filter (keeps real sibling qual*/supp*/pa* files, and any
    unrelated/corrupt XML, out)."""
    candidates = list_input_files(raw_dir, _CANDIDATE_PATTERNS)
    files = [p for p in candidates if _is_descriptor_file(p)]
    files.sort(key=lambda p: p.name)
    return files


def _best_effort_audit(basename: str, n_rows: int) -> None:
    """Mirror serialize_chembl.py / serialize_pubchem.py / serialize_clinvar
    .py / serialize_reactome.py's ``_best_effort_audit`` shape verbatim,
    including the settled ``event_type="serialize_commit"`` ruling (NOT
    ``"extract_commit"`` -- settled by Tasks 4/5/6, not re-litigated here).

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


def serialize_mesh(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Importable core: parse MeSH descriptor XML files under ``raw_dir``
    into staging shards + ops markers under ``processed_dir``.

    Returns ``{"inputs": n, "ok": n, "failed": n, "rows": n}``.
    """
    raw_dir = Path(raw_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    files = discover_mesh_files(raw_dir)
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
    files = discover_mesh_files(raw_dir)
    if max_files and max_files > 0:
        files = files[:max_files]
    if not files:
        print(f"ERROR: no MeSH descriptor files under {raw_dir}", file=sys.stderr)
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
        prog="python -m episteme.data.mesh.serialize_mesh",
        description="MeSH descriptor XML (desc<year>.xml[.gz]) to episteme.articles staging shards",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "mesh")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument(
        "--max-files",
        type=int,
        default=settings.sample_limit,
        help="0 = all discovered descriptor files (env: EPISTEME_SAMPLE_LIMIT)",
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

    res = serialize_mesh(
        raw_dir,
        processed_dir,
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )

    if res["inputs"] == 0:
        print(f"ERROR: no MeSH descriptor files under {raw_dir}", file=sys.stderr)
        return 1

    print(
        f"done inputs={res['inputs']} ok={res['ok']} " f"failed={res['failed']} rows={res['rows']}"
    )
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
