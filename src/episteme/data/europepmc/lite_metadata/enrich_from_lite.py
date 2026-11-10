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
semantics). The UPDATE has a WHERE predicate, so a row is rewritten only when
a column would change (no new tuple version for a no-op match).
``rows_matched`` counts the pmcids seen in the feed; ``rows_updated`` is the
sum of ``cur.rowcount`` (rows that changed).

Restartability: with ``processed_root`` and ``raw_dir`` the pass commits per
tar member (and every ``commit_batches`` batches inside a member) and writes a
marker per finished member, keyed on ``input_key(tgz, raw_dir)``, the member's
position in the archive and the percent-encoded member name. The marker also holds
the target database (name, server address, port) and the run time: a marker made
against another database does not count. A re-run skips finished members, so after
new articles were loaded, ``force`` is needed to enrich them from a finished member.
The UPDATE is idempotent, so a member that stopped half way is safe to redo.

This module never reads ``os.environ`` -- ``enrich_from_lite`` takes a
``conn`` directly (the caller, e.g. ``main()``, owns DSN resolution via
``episteme.data.db.connection.connection()`` / ``episteme.config``).
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import iterparse as _safe_iterparse

from episteme.data import checkpoint_markers
from episteme.data.article_schema import cap_list_elements

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
    return cap_list_elements(terms) or None


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


def _iter_members(tgz_path: Path):
    """Yield ``(ordinal, member name, records)`` for every XML tar member of ``tgz_path``, in
    streaming mode (``"r|gz"`` -- forward-only, no seeking: the feed is 2.2GB compressed
    / tens of GB uncompressed). The caller must use or drop ``records`` before it asks
    for the next member; a member that is not iterated is skipped by the stream."""
    ordinal = 0
    with tarfile.open(tgz_path, "r|gz") as tf:
        for member in tf:
            if not member.isfile() or not member.name.endswith(".xml"):
                continue
            fileobj = tf.extractfile(member)
            if fileobj is None:
                continue
            ordinal += 1
            yield ordinal, member.name, _iter_records(fileobj)


def member_key(tgz_path: Path, raw_dir: Path, member: str, ordinal: int = 0) -> str:
    """Marker name of one tar member: ``input_key`` of the tgz, the member's position in the
    archive (two members can share a name) and the percent-encoded member name (``/`` becomes
    ``%2F`` and ``%`` becomes ``%25``, so two member names never give the same key)."""
    base = checkpoint_markers.input_key(tgz_path, raw_dir)
    return f"{base}__member{ordinal}__{quote(member, safe='')}"


_UPDATE_SQL = """
UPDATE episteme.articles AS a
   SET journal = coalesce(a.journal, v.journal),
       year    = coalesce(CASE WHEN a.year IS NULL OR a.year = 0 THEN v.year
                               ELSE a.year END, 0),
       mesh    = CASE WHEN a.mesh IS NULL OR a.mesh = '{}' THEN v.mesh ELSE a.mesh END
  FROM (VALUES %s) AS v (pmcid, journal, year, mesh)
 WHERE a.pmcid = v.pmcid
   AND ((a.journal IS NULL AND v.journal IS NOT NULL)
        OR ((a.year IS NULL OR a.year = 0) AND v.year IS NOT NULL AND v.year <> 0)
        OR ((a.mesh IS NULL OR a.mesh = '{}') AND v.mesh IS NOT NULL AND v.mesh <> '{}'))
"""


def _flush(cur, batch: list[dict[str, Any]]) -> int:
    """Issue one set-based UPDATE for ``batch`` (a list of {pmcid, journal,
    year, mesh} dicts) and return the number of ``episteme.articles`` rows it
    changed (the WHERE predicate leaves a row alone when nothing would change).

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


def _fingerprint(tgz_path: Path) -> dict[str, int]:
    st = tgz_path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _db_identity(conn) -> str:
    """Name, server address and port of the connected database (no credentials). A
    marker made against another database must not count."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), inet_server_addr()::text, inet_server_port()")
        row = cur.fetchone()
    return "|".join(str(v) for v in row) if row else "unknown"


def _member_done(marker: Path, fp: dict[str, int], database: str) -> bool:
    """A marker counts only for the same file (size and mtime) and the same target
    database: a new weekly file with the same name, or another database, is processed."""
    if not marker.is_file():
        return False
    try:
        stats = json.loads(marker.read_text(encoding="utf-8")).get("stats", {})
    except (OSError, ValueError):
        return False
    return stats.get("file") == fp and stats.get("database") == database


def enrich_from_lite(
    tgz_path: Path,
    conn,
    *,
    force: bool = False,
    processed_root: Path | None = None,
    raw_dir: Path | None = None,
    batch_size: int | None = None,
    commit_batches: int | None = None,
) -> dict[str, Any]:
    """Enrich ``episteme.articles`` from one ``PMCLiteMetadata.tgz`` file.

    Without ``processed_root`` the pass runs in the caller's transaction (no commit, no
    marker). With ``processed_root`` (and ``raw_dir``, default the tgz folder) it commits
    after every ``commit_batches`` batches and at the end of each tar member, and writes a
    marker per finished member; finished members are skipped unless ``force``.
    ``batch_size`` and ``commit_batches`` default to the Settings values.

    Returns ``{"records_read", "rows_matched", "rows_updated", "members_done",
    "members_skipped"}``. ``rows_matched`` counts the pmcids seen in the feed in the
    members read; ``rows_updated`` is the number of rows that changed (the WHERE
    predicate of ``_UPDATE_SQL`` skips a row that would not change).
    """
    from episteme.config import get_settings

    tgz_path = Path(tgz_path)
    settings = get_settings()
    batch_size = batch_size or settings.lite_batch_size
    commit_batches = commit_batches or settings.lite_commit_batches
    raw_dir = Path(raw_dir) if raw_dir is not None else tgz_path.parent
    fp = _fingerprint(tgz_path) if processed_root is not None else {}
    database = _db_identity(conn) if processed_root is not None else ""

    records_read = rows_matched = rows_updated = done = skipped = 0
    with conn.cursor() as cur:
        for ordinal, name, records in _iter_members(tgz_path):
            marker = None
            if processed_root is not None:
                marker = checkpoint_markers.load_success_marker_path(
                    Path(processed_root), SOURCE, member_key(tgz_path, raw_dir, name, ordinal)
                )
                if not force and _member_done(marker, fp, database):
                    skipped += 1
                    continue
            batch: list[dict[str, Any]] = []
            batches = member_read = 0
            for rec in records:
                member_read += 1
                batch.append(rec)
                if len(batch) >= batch_size:
                    rows_updated += _flush(cur, batch)
                    batch = []
                    batches += 1
                    if processed_root is not None and batches % commit_batches == 0:
                        conn.commit()
            rows_updated += _flush(cur, batch)
            records_read += member_read
            rows_matched += member_read
            if marker is not None:
                conn.commit()
                checkpoint_markers.write_marker(
                    marker,
                    {
                        "source": SOURCE,
                        "member": name,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "stats": {
                            "file": fp,
                            "database": database,
                            "run_at": datetime.now(timezone.utc).isoformat(),
                            "records": member_read,
                        },
                    },
                )
            done += 1
    return {
        "records_read": records_read,
        "rows_matched": rows_matched,
        "rows_updated": rows_updated,
        "members_done": done,
        "members_skipped": skipped,
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
        help="redo tar members that already have a finished marker",
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
        result = enrich_from_lite(
            tgz_path,
            conn,
            force=args.force,
            processed_root=settings.processed_root,
            raw_dir=args.raw_dir,
        )
        conn.commit()

    print(
        f"done records_read={result['records_read']} rows_matched={result['rows_matched']} "
        f"rows_updated={result['rows_updated']} members_done={result['members_done']} "
        f"members_skipped={result['members_skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
