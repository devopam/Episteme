"""Enrich ``episteme.articles`` (``journal`` / ``year`` / ``mesh``) from Europe
PMC's ``PMCLiteMetadata.tgz`` feed. Task 12: an UPDATE-only enrichment pass,
NOT an article extractor -- no ``article_schema`` / ``jats.py`` involvement, no
staging shard, no field-shape ``--report``. Mirrors Task 11's
``load_id_mappings.py`` in shape (``pg``-gated, its own ``run_pipeline.sh``
dispatch exception) but is ``enrich`` + UPDATE instead of ``load`` + INSERT.

Real file shape (confirmed 2026-09-11 via HTTP range reads of
``https://ftp.ebi.ac.uk/pub/databases/pmc/PMCLiteMetadata/PMCLiteMetadata.tgz``,
2.2GB compressed):
  * NOT per-PMC members. A handful (~20, observed ``out/PMC.1.xml`` and
    ``out/PMC.16.xml`` so far) of large tar members, each a single XML
    document: ``<PMCSet><PMC_ARTICLE>...</PMC_ARTICLE><PMC_ARTICLE>...
    </PMC_ARTICLE>...</PMCSet>``. The first member alone is 821MB
    uncompressed (~41.7k records observed in a 62MB decompressed slice of
    it); the full feed is tens of millions of records across all members.
  * Per-record fields observed: ``id``, ``source``, ``pmid``, ``pmcid``,
    ``DOI``, ``title``, ``AuthorList``, ``JournalTitle``, ``Issue``,
    ``JournalVolume``, ``PubYear``, ``JournalIssn``, ``PageInfo``, ``PubType``,
    ``IsOpenAccess``, ``InEPMC``, ``InPMC``, ``HasPDF``, ``HasBook``,
    ``HasSuppl``, ``CitedByCount``, ``HasReferences``, ``HasLabsLinks``,
    ``FirstIndexDate``, ``FirstPublicationDate``, ``PublicationStatus``.
  * NO MeSH data anywhere in the observed sample (~41.7k records) -- this is
    consistent with Europe PMC's documented ``resultType=lite`` vs.
    ``resultType=core`` REST API split: ``core`` adds a
    ``meshHeadingList``/``chemicalList``/``subsetList`` block that ``lite``
    omits (confirmed by comparing against a live ``resultType=core`` REST
    call), and ``PMCLiteMetadata.tgz`` is exactly the bulk ``lite`` dump.
    ``_mesh_terms`` below still parses an optional
    ``<meshHeadingList><meshHeading><descriptorName>`` block defensively (it
    costs nothing and keeps the brief's mandated ``mesh`` UPDATE clause live
    for a future core-shaped dump), but against the REAL lite feed this
    always resolves to ``None`` -- ``mesh`` will not be filled from a real
    run of this module. See the task report for confirmation against a real
    slice.

Scale note: given the tens-of-millions-of-record scale, this module does NOT
build the brief's sketched in-memory ``{pmcid: {...}}`` dict (which would be
multi-GB of RAM for the full feed). It streams instead: ``tarfile.open(...,
"r|gz")`` in streaming mode (single forward pass, no seeking -- required for a
2.2GB compressed / tens-of-GB uncompressed feed) over each member's file
object, ``xml.etree.ElementTree.iterparse`` per member clearing the parsed
tree as it goes (memory stays flat regardless of file size), batching UPDATE
parameters and flushing a single set-based ``UPDATE ... FROM (VALUES ...)``
per batch (faster for large N than one ``UPDATE`` per row, and gives an exact
``cur.rowcount`` per batch, unlike ``executemany``'s driver-dependent rowcount
semantics). Because records are only ever turned into batched UPDATE params
(never stored in a dict), ``rows_matched`` and ``rows_updated`` collapse into
the same number here -- an UPDATE is issued only for pmcids seen in the feed,
so "matched" and "had an UPDATE issued for it" are the same population; see
``enrich_from_lite``'s docstring.

This module never reads ``os.environ`` -- ``enrich_from_lite`` takes a
``conn`` directly (the caller, e.g. ``main()``, owns DSN resolution via
``episteme.data.db.connection.connection()`` / ``episteme.config``).
"""

from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import iterparse as _safe_iterparse

# Batch size for the set-based UPDATE ... FROM (VALUES ...) flush. Kept modest
# (not tens of thousands) so a single UPDATE statement's parameter count and
# VALUES-list size stay comfortably within psycopg/Postgres limits.
BATCH_SIZE = 2000

SOURCE = "europepmc_lite"


def _text(elem: ET.Element, tag: str) -> str | None:
    child = elem.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _year(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _mesh_terms(elem: ET.Element) -> list[str] | None:
    """Optional ``<meshHeadingList><meshHeading><descriptorName>`` block --
    never observed in the real ``PMCLiteMetadata.tgz`` feed (see module
    docstring) but parsed defensively for forward-compatibility with a
    ``core``-shaped dump."""
    mesh_list = elem.find("meshHeadingList")
    if mesh_list is None:
        return None
    terms = [
        d.text.strip()
        for d in mesh_list.findall("meshHeading/descriptorName")
        if d.text and d.text.strip()
    ]
    return terms or None


def _iter_records(fileobj: IO[bytes]):
    """Stream ``<PMC_ARTICLE>`` records out of one tar member's file object,
    one at a time. Clears the parsed tree as it goes (``elem.clear()`` plus
    dropping the now-fully-consumed child from the ``<PMCSet>`` root) so
    memory stays flat regardless of member size -- required at this feed's
    real scale (a single member is ~800MB uncompressed)."""
    context = iter(_safe_iterparse(fileobj, events=("start", "end")))
    _, root = next(context)
    for event, elem in context:
        if event != "end" or elem.tag != "PMC_ARTICLE":
            continue
        pmcid = _text(elem, "pmcid")
        if pmcid:
            yield {
                "pmcid": pmcid,
                "journal": _text(elem, "JournalTitle"),
                "year": _year(_text(elem, "PubYear")),
                "mesh": _mesh_terms(elem),
            }
        elem.clear()
        root.clear()


def _iter_all_records(tgz_path: Path):
    """Stream every ``<PMC_ARTICLE>`` record across every tar member of
    ``tgz_path``, in streaming mode (``"r|gz"`` -- forward-only, no seeking:
    the feed is 2.2GB compressed / tens of GB uncompressed, too large to load
    or seek within)."""
    with tarfile.open(tgz_path, "r|gz") as tf:
        for member in tf:
            if not member.isfile() or not member.name.endswith(".xml"):
                continue
            fileobj = tf.extractfile(member)
            if fileobj is None:
                continue
            yield from _iter_records(fileobj)


_UPDATE_SQL = """
UPDATE episteme.articles AS a
   SET journal = coalesce(a.journal, v.journal),
       year    = CASE WHEN a.year IS NULL OR a.year = 0 THEN v.year ELSE a.year END,
       mesh    = CASE WHEN a.mesh IS NULL OR a.mesh = '{}' THEN v.mesh ELSE a.mesh END
  FROM (VALUES %s) AS v (pmcid, journal, year, mesh)
 WHERE a.pmcid = v.pmcid
"""


def _flush(cur, batch: list[dict[str, Any]]) -> int:
    """Issue one set-based UPDATE for ``batch`` (a list of {pmcid, journal,
    year, mesh} dicts) and return the number of ``episteme.articles`` rows it
    matched (== updated-or-no-op'd, since ``coalesce``/the mesh ``CASE`` may
    leave a row's values unchanged if it already had them).

    Known edge case, accepted rather than engineered around: if the SAME
    pmcid appears twice within one batch (e.g. a correction/duplicate record
    in the feed), Postgres' ``UPDATE ... FROM`` does not error on multiple
    matching source rows -- it applies one of them, nondeterministically as
    to which. Not observed in the real feed's sampled slice; harmless in
    practice either way since both would carry near-identical
    journal/year/mesh and the UPDATE is coalesce-only (fills empty columns,
    never overwrites)."""
    if not batch:
        return 0
    values_sql = ", ".join(["(%s::text, %s::text, %s::int, %s::text[])"] * len(batch))
    params: list[Any] = []
    for row in batch:
        params.extend((row["pmcid"], row["journal"], row["year"], row["mesh"]))
    cur.execute(_UPDATE_SQL % values_sql, params)
    return cur.rowcount


def enrich_from_lite(tgz_path: Path, conn, *, force: bool = False) -> dict[str, Any]:
    """Enrich ``episteme.articles`` from one ``PMCLiteMetadata.tgz`` file.

    Runs in the caller's transaction (does not commit).

    Returns:
        {"records_read": int, "rows_matched": int, "rows_updated": int}

    ``rows_matched`` and ``rows_updated`` are the same number here: an UPDATE
    is issued only for pmcids actually seen in the feed (a pmcid with no
    matching lite-metadata record gets no UPDATE at all -- not even a 0-row
    no-op one), so "matched" (found in the feed) and "had an UPDATE issued for
    it, whether or not it changed anything" are identical populations. Kept as
    two dict keys per the brief's contract, in case a future caller wants to
    distinguish "found" from "actually changed" (this module doesn't, per
    coalesce()/CASE semantics -- a match that already had journal/year/mesh
    set still counts as matched+updated even though the row's values didn't
    change).

    ``force`` is accepted for CLI-flag symmetry with the other SP2 enrichment
    passes (Task 11's ``load_id_mappings``) but is a no-op: there is no
    checkpoint/marker system for this one-shot UPDATE pass, and
    ``coalesce(journal, ...)``/the mesh ``CASE`` only ever fill NULL/empty
    columns, so a re-run of an unchanged file is naturally idempotent (same
    reasoning as Task 11).
    """
    tgz_path = Path(tgz_path)
    _ = force  # no-op: coalesce()-based UPDATE is already idempotent (see docstring)

    records_read = 0
    rows_matched = 0
    batch: list[dict[str, Any]] = []

    with conn.cursor() as cur:
        for rec in _iter_all_records(tgz_path):
            records_read += 1
            batch.append(rec)
            if len(batch) >= BATCH_SIZE:
                rows_matched += _flush(cur, batch)
                batch = []
        rows_matched += _flush(cur, batch)

    return {
        "records_read": records_read,
        "rows_matched": rows_matched,
        "rows_updated": rows_matched,
    }


def main(argv: list[str] | None = None) -> int:
    from episteme.config import get_settings
    from episteme.data.db.connection import connection

    settings = get_settings()

    p = argparse.ArgumentParser(
        prog="python -m episteme.data.europepmc.lite_metadata.enrich_from_lite",
        description=(
            "Enrich episteme.articles (journal/year/mesh) from Europe PMC PMCLiteMetadata.tgz."
        ),
    )
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=settings.raw_root / "europepmc" / "lite_metadata",
        help="directory holding the downloaded *.tgz (default: 01_raw/europepmc/lite_metadata)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="accepted for CLI symmetry; no-op (coalesce()-based UPDATE is already idempotent)",
    )
    p.add_argument(
        "--reason",
        type=str,
        default=None,
        help="accepted for CLI symmetry with run_pipeline.sh's --force --reason; unused",
    )
    args = p.parse_args(argv)

    raw_dir: Path = args.raw_dir
    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    candidates = sorted(raw_dir.glob("*.tgz"))
    if not candidates:
        print(f"ERROR: no *.tgz under {raw_dir}", file=sys.stderr)
        return 1
    if len(candidates) > 1:
        print(
            f"ERROR: multiple *.tgz under {raw_dir}, expected exactly one: "
            f"{[c.name for c in candidates]}",
            file=sys.stderr,
        )
        return 1
    tgz_path = candidates[0]

    print(f"source={SOURCE} tgz_path={tgz_path}")
    with connection() as conn:
        result = enrich_from_lite(tgz_path, conn, force=args.force)
        conn.commit()

    print(
        f"done records_read={result['records_read']} rows_matched={result['rows_matched']} "
        f"rows_updated={result['rows_updated']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
