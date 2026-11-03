"""Load FDA's substance data (``other/substance``) into ``substance`` and ``substance_xref``.

    python -m episteme.data.regulatory.load_substance [--raw-dir DIR] [--force --reason "..."]

The substance data is the anchor of the US/EU bridge. One record has a UNII, its
names (every synonym), its codes and its relationships. The loader writes:

* one ``substance`` row per UNII (``preferred_name``, ``moiety_key``);
* ``substance_xref`` rows for the ``unii``, every ``name`` (normalised with
  ``norm_name``), the ``rxcui`` and the ``atc`` code of each record.

The file is one 2.4 GB JSON document inside a zip. ``iter_substances`` reads it as a
stream (``ijson``); ``load_substances`` ``COPY``s the rows into temporary tables and
merges them with set-based SQL, so no step holds the records or their names in
Python memory. The order of the records does not matter: a moiety UNII with no record
of its own gets its own ``substance`` row, so ``moiety_key`` always points at a row.

Keys are stable: ``substance_key`` comes from an identity column, a UNII keeps its key
on reload, and an xref row is added with ``ON CONFLICT DO NOTHING``, never moved.

``moiety_of`` takes the first ``ACTIVE MOIETY`` relationship whose UNII differs from
the record's own. A record can have more than one (a salt of two moieties) and often
has one that points at itself; the record's own UNII is the fallback.

``resolve_names`` maps names from another dataset to substances by exact normalised match,
and counts the names that do not match. No fuzzy match. ``load_reg`` calls it after each
good EMA release (source ``ema``, rows ``ema_name``); FAERS has its own bridge
(``faers_entry_substance``).

A file is skipped only when its marker, keyed on the database name and the ``input_key``,
matches the file and the database holds this dataset's rows.

The CLI connects as the table owner, as ``load_reg`` does. Config comes from
``episteme.config``; this module never reads ``os.environ``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ijson
import psycopg

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data import checkpoint_markers
from episteme.data.regulatory.load_reg import database_name, db_marker_name, input_keys
from episteme.data.regulatory.normalize import MAX_INDEX_TEXT_BYTES, norm_name
from episteme.ops.failure import record_failure

_LOG = logging.getLogger(__name__)

SCHEMA = "episteme"
SOURCE = "openfda"
DATASET = "fda_substance"
# A name longer than this (some chemical names) is skipped and counted; it cannot be
# indexed in the xref key.
MAX_NAME_BYTES = MAX_INDEX_TEXT_BYTES
_ATC_SYSTEM = "WHO-ATC"  # WHO-VATC holds the veterinary codes and is left out
_RXCUI_SYSTEM = "RXCUI"
_ACTIVE_MOIETY = "ACTIVE MOIETY"


# ---------------------------------------------------------------------------
# Reading (pure)
# ---------------------------------------------------------------------------
def iter_substances(path: Path) -> Iterator[dict]:
    """The records of the zipped substance JSON, one at a time (``results.item``).
    The 2.4 GB document is never loaded whole."""
    with zipfile.ZipFile(path) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith(".json")]
        if len(members) != 1:
            raise ValueError(f"{path}: expected one .json member, found {len(members)}")
        with zf.open(members[0]) as fh:
            yield from ijson.items(fh, "results.item", use_float=True)


def moiety_of(record: dict) -> str | None:
    """The UNII of the active moiety of ``record``: the first ``ACTIVE MOIETY``
    relationship whose related UNII differs from the record's own, else the record's
    own UNII (``None`` when it has none)."""
    own = _unii(record.get("unii"))
    for rel in record.get("relationships") or []:
        if rel.get("type") != _ACTIVE_MOIETY:
            continue
        related = _unii((rel.get("related_substance") or {}).get("unii"))
        if related and related != own:
            return related
    return own


def _clean(value: Any) -> str:
    return str(value).replace("\x00", "").strip()


def _unii(value: Any) -> str | None:
    """A UNII without NUL and outer space; ``None`` when nothing is left."""
    return _clean(value) or None if value is not None else None


def preferred_name(record: dict) -> str | None:
    names = [n for n in record.get("names") or [] if _clean(n.get("name") or "")]
    chosen = next((n for n in names if n.get("preferred")), names[0] if names else None)
    return _clean(chosen["name"]) if chosen else None


def record_xrefs(record: dict) -> tuple[list[tuple[str, str]], int]:
    """The ``(id_type, id_value)`` pairs of one record (without the UNII itself) and
    the number of names skipped for length."""
    out: dict[tuple[str, str], None] = {}
    skipped = 0
    for n in record.get("names") or []:
        value = norm_name(_clean(n.get("name") or ""))
        if not value:
            continue
        if len(value.encode("utf-8")) > MAX_NAME_BYTES:
            skipped += 1
            continue
        out[("name", value)] = None
    for c in record.get("codes") or []:
        system, code = c.get("code_system"), _clean(c.get("code") or "")
        if not code:
            continue
        if system == _RXCUI_SYSTEM:
            out[("rxcui", code)] = None
        elif system == _ATC_SYSTEM:
            out[("atc", code.upper())] = None
    return list(out), skipped


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# One row per UNII. When a UNII appears twice, the first stage row with a name wins, and
# the name and the moiety always come from that same row.
_SUB = (
    "(SELECT DISTINCT ON (unii) unii, id_value AS preferred_name, aux AS moiety_unii "
    "FROM _stg WHERE id_type = '_sub' ORDER BY unii, (id_value IS NULL), seq)"
)
_METHOD_SQL = "CASE x.id_type WHEN 'name' THEN 'normalised_name' ELSE 'exact_code' END"


def load_substances(conn: psycopg.Connection, path: Path, *, run_id: str) -> dict:
    """Load one substance file; the caller commits. Returns counts:
    ``records``, ``no_unii`` (records with no UNII, skipped), ``substances``
    (distinct UNIIs in the file), ``moiety_stubs`` (moieties with no record of their
    own), ``multi_moiety`` (records with several distinct active moieties),
    ``long_names`` (skipped), ``xref_added`` (new rows), ``xref_removed`` (rows of this
    dataset that are no longer in the file)."""
    stats = {"records": 0, "no_unii": 0, "multi_moiety": 0, "long_names": 0}
    with conn.transaction():
        with conn.cursor() as cur:
            # One COPY at a time per connection, so one stage table: a row with
            # id_type '_sub' carries (preferred name, moiety UNII); the others are xrefs.
            cur.execute("DROP TABLE IF EXISTS pg_temp._stg")
            cur.execute(
                "CREATE TEMP TABLE _stg (unii text, id_type text, id_value text, aux text, "
                "seq bigserial) "
                "ON COMMIT DROP"
            )
            with cur.copy("COPY _stg (unii, id_type, id_value, aux) FROM STDIN") as cp:
                for rec in iter_substances(path):
                    stats["records"] += 1
                    unii = _unii(rec.get("unii"))
                    if not unii:
                        stats["no_unii"] += 1
                        continue
                    moieties = {
                        _unii((r.get("related_substance") or {}).get("unii"))
                        for r in rec.get("relationships") or []
                        if r.get("type") == _ACTIVE_MOIETY
                    } - {None, unii}
                    if len(moieties) > 1:
                        stats["multi_moiety"] += 1
                    cp.write_row((unii, "_sub", preferred_name(rec), moiety_of(rec)))
                    pairs, skipped = record_xrefs(rec)
                    stats["long_names"] += skipped
                    cp.write_row((unii, "unii", unii, None))
                    for id_type, id_value in pairs:
                        cp.write_row((unii, id_type, id_value, None))
            cur.execute(
                f"INSERT INTO {SCHEMA}.substance (unii, preferred_name) "
                f"SELECT DISTINCT ON (unii) unii, preferred_name FROM {_SUB} sub "
                "ON CONFLICT (unii) WHERE unii IS NOT NULL DO UPDATE SET "
                "preferred_name = COALESCE(EXCLUDED.preferred_name, substance.preferred_name)"
            )
            cur.execute("SELECT count(DISTINCT unii) FROM _stg WHERE id_type = '_sub'")
            stats["substances"] = cur.fetchone()[0]
            # A moiety with no record of its own still gets a row.
            cur.execute(
                f"INSERT INTO {SCHEMA}.substance (unii) "
                f"SELECT DISTINCT moiety_unii FROM {_SUB} sub WHERE moiety_unii IS NOT NULL "
                "ON CONFLICT (unii) WHERE unii IS NOT NULL DO NOTHING"
            )
            stats["moiety_stubs"] = cur.rowcount
            # Every moiety UNII is looked up by its own UNII, record or not.
            cur.execute(
                "INSERT INTO _stg (unii, id_type, id_value) "
                f"SELECT DISTINCT moiety_unii, 'unii', moiety_unii FROM {_SUB} sub "
                "WHERE moiety_unii IS NOT NULL"
            )
            cur.execute(
                f"UPDATE {SCHEMA}.substance s SET moiety_key = m.substance_key "
                f"FROM {_SUB} st "
                f"JOIN {SCHEMA}.substance m ON m.unii = st.moiety_unii "
                "WHERE s.unii = st.unii AND s.moiety_key IS DISTINCT FROM m.substance_key"
            )
            cur.execute(
                f"UPDATE {SCHEMA}.substance SET moiety_key = substance_key "
                "WHERE moiety_key IS NULL AND unii IS NOT NULL"
            )
            cur.execute(
                f"INSERT INTO {SCHEMA}.substance_xref "
                "(substance_key, id_type, id_value, method, confidence, source) "
                f"SELECT DISTINCT s.substance_key, x.id_type, x.id_value, {_METHOD_SQL}, "
                "1.0, %s::text "
                f"FROM _stg x JOIN {SCHEMA}.substance s "
                "ON s.unii = x.unii AND x.id_type <> '_sub' "
                "ON CONFLICT (id_type, id_value, substance_key) DO NOTHING",
                (DATASET,),
            )
            stats["xref_added"] = cur.rowcount
            # A reload replaces this dataset's rows: one that is gone from the file goes
            # (a dropped or reassigned name would otherwise look like a combination).
            # Rows of other sources, and every substance row and key, stay.
            cur.execute(
                f"DELETE FROM {SCHEMA}.substance_xref r WHERE r.source = %s::text AND NOT EXISTS "
                f"(SELECT 1 FROM _stg x JOIN {SCHEMA}.substance s ON s.unii = x.unii "
                "WHERE x.id_type <> '_sub' AND s.substance_key = r.substance_key "
                "AND x.id_type = r.id_type AND x.id_value = r.id_value)",
                (DATASET,),
            )
            stats["xref_removed"] = cur.rowcount
        audit_trail.record(
            "load_commit",
            conn=conn,
            object=DATASET,
            rows_affected=stats["substances"],
            run_id=run_id,
        )
    return stats


def resolve_names(conn: psycopg.Connection, names: Iterable[str], source: str) -> dict:
    """Map ``names`` (from ``source``, e.g. ``ema``) to substances by exact normalised
    match against the ``name`` rows. Each match adds a ``<source>_name`` xref row
    (method ``normalised_name``); a name that matches several substances (a
    combination) adds a row for each. Returns ``{"matched", "unmatched",
    "skipped_long"}``: distinct normalised names, except that a name longer than
    ``MAX_NAME_BYTES`` counts once per occurrence, as unmatched and in ``skipped_long``.
    Nothing is guessed; the caller commits."""
    id_type = f"{source}_name"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS pg_temp._stg_names, pg_temp._stg_distinct")
            cur.execute("CREATE TEMP TABLE _stg_names (name text) ON COMMIT DROP")
            skipped_long = 0
            with cur.copy("COPY _stg_names FROM STDIN") as cp:
                for raw in names:
                    n = norm_name(_clean(raw or ""))
                    if not n:
                        continue
                    if len(n.encode("utf-8")) > MAX_NAME_BYTES:
                        skipped_long += 1
                        continue
                    cp.write_row((n,))
            cur.execute(
                "CREATE TEMP TABLE _stg_distinct ON COMMIT DROP AS "
                "SELECT DISTINCT name FROM _stg_names"
            )
            # A re-resolved name loses this source's earlier rows first, so a name that
            # moved does not stay on its old substance. Other sources' rows stay.
            cur.execute(
                f"DELETE FROM {SCHEMA}.substance_xref WHERE id_type = %s::text "
                "AND source = %s::text AND id_value IN (SELECT name FROM _stg_distinct)",
                (id_type, source),
            )
            cur.execute(
                f"INSERT INTO {SCHEMA}.substance_xref "
                "(substance_key, id_type, id_value, method, confidence, source) "
                "SELECT DISTINCT x.substance_key, %s::text, d.name, 'normalised_name', "
                "1.0, %s::text "
                f"FROM _stg_distinct d JOIN {SCHEMA}.substance_xref x "
                "  ON x.id_type = 'name' AND x.id_value = d.name "
                "ON CONFLICT (id_type, id_value, substance_key) DO NOTHING",
                (id_type, source),
            )
            cur.execute(
                "SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM "
                f"{SCHEMA}.substance_xref x WHERE x.id_type = 'name' AND x.id_value = d.name)), "
                "count(*) FROM _stg_distinct d"
            )
            matched, total = cur.fetchone()
    # A name too long to index cannot match: it is counted as unmatched (and apart).
    return {
        "matched": matched,
        "unmatched": total - matched + skipped_long,
        "skipped_long": skipped_long,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def substance_files(raw_dir: Path) -> list[Path]:
    """The substance zips under ``<raw>/other/substance/``, in path order."""
    base = raw_dir / "other" / "substance"
    return sorted(p for p in base.rglob("*.zip") if p.is_file()) if base.is_dir() else []


def marker_name_for(path: Path, raw_dir: Path) -> str:
    return checkpoint_markers.input_key(path, raw_dir)


def substances_loaded(conn: psycopg.Connection) -> bool:
    """Whether the database holds ``substance_xref`` rows of this dataset. A marker alone
    never proves the load. The read is rolled back; a failed read counts as "not loaded"."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.substance_xref WHERE source = %s)",
                (DATASET,),
            )
            return bool(cur.fetchone()[0])
    except psycopg.Error:
        _LOG.warning("could not read substance_xref", exc_info=True)
        return False
    finally:
        conn.rollback()


def _fingerprint(path: Path) -> list:
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def run_files(
    conn: psycopg.Connection,
    files: list[Path],
    raw_dir: Path,
    processed_root: Path,
    *,
    force: bool,
    reason: str | None,
    run_id: str,
) -> dict:
    """Load each file. A file is skipped (unless ``force``) only when its marker for this
    database matches the file AND the database holds this dataset's xref rows
    (``substances_loaded``); otherwise it loads, whatever the marker says.
    Returns ``{"loaded", "skipped", "failed"}`` (``failed`` lists the input keys)."""
    keys = input_keys(files, raw_dir)  # raises when two inputs share a key
    loaded = skipped = 0
    failed: list[str] = []
    if len(files) > 1:
        # The reload deletes this dataset's rows that the file lacks, so the file must be
        # the whole dataset. Refuse before any write.
        why = f"{len(files)} substance files found; the load needs exactly one: {keys}"
        print(f"FAIL {DATASET}: {why}")
        try:
            record_failure(SOURCE, "load", DATASET, why, run_id=run_id)
        except Exception:  # noqa: BLE001 - the failed list below still carries it
            _LOG.exception("could not record the failure of %s", DATASET)
        return {"loaded": 0, "skipped": 0, "failed": [DATASET]}
    if not files:
        print(f"FAIL {DATASET}: no input files under {raw_dir / 'other' / 'substance'}")
        failed.append(DATASET)
    database = database_name(conn) if files else ""
    for path, key in zip(files, keys, strict=True):
        marker = checkpoint_markers.load_success_marker_path(
            processed_root, SOURCE, db_marker_name(database, key)
        )
        fp = _fingerprint(path)
        if marker.is_file() and not force:
            try:
                stored = json.loads(marker.read_text(encoding="utf-8")).get("stats", {})
            except (OSError, ValueError):
                stored = {}
            if stored.get("file") == fp:
                if substances_loaded(conn):
                    print(f"skip {key}")
                    skipped += 1
                    continue
                print(f"reload {key}: a marker exists, but {database} holds no {DATASET} rows")
        try:
            try:
                with audit_trail.deferred_mirror(conn):
                    if force:
                        audit_trail.record(
                            "force_override", conn=conn, object=key, reason=reason, run_id=run_id
                        )
                    stats = load_substances(conn, path, run_id=run_id)
                    audit_trail.commit(conn)
            except BaseException:
                audit_trail.rollback(conn)
                raise
            checkpoint_markers.write_marker(
                marker,
                {
                    "source": SOURCE,
                    "source_file": key,
                    "database": database,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "stats": {**stats, "file": fp},
                },
            )
        except Exception as exc:  # noqa: BLE001 - report and go on to the next file
            print(f"FAIL {key}: {exc!r}")
            try:
                record_failure(SOURCE, "load", key, repr(exc), run_id=run_id)
            except Exception:  # noqa: BLE001 - the list below still carries it
                _LOG.exception("could not record the failure of %s", key)
            failed.append(key)
            continue
        loaded += 1
        print(
            f"ok {key} records={stats['records']} substances={stats['substances']} "
            f"no_unii={stats['no_unii']} moiety_stubs={stats['moiety_stubs']} "
            f"multi_moiety={stats['multi_moiety']} long_names={stats['long_names']} "
            f"xref_added={stats['xref_added']}"
        )
    return {"loaded": loaded, "skipped": skipped, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    from episteme.data.db import connection as db_connection
    from episteme.data.db.guard import pool_kwargs
    from episteme.ops.status import current
    from episteme.ops.stop import stage_guard

    parser = argparse.ArgumentParser(
        prog="python -m episteme.data.regulatory.load_substance",
        description="Load FDA substance data into episteme.substance and substance_xref.",
    )
    parser.add_argument("--raw-dir", type=Path, default=None, help="default: <raw root>/openfda")
    parser.add_argument("--force", action="store_true", help="reload files with a marker")
    parser.add_argument("--reason", default=None, help="required with --force (audited)")
    args = parser.parse_args(argv)
    if args.force and not args.reason:
        print("error: --force requires --reason", file=sys.stderr)
        return 2

    settings = get_settings()
    raw_dir = args.raw_dir or (settings.raw_root / SOURCE)
    files = substance_files(raw_dir)
    external_run_id = settings.run_id
    run_id = external_run_id or f"{SOURCE}-substance-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    own_bracket = external_run_id is None
    dsn = db_connection.admin_dsn_from_settings()
    holder: list = [None]
    outcome: dict = {"loaded": 0, "skipped": 0, "failed": []}

    with stage_guard(lambda: holder[0], lock_stage="load", db_lock=True, argv=argv, dsn=dsn) as g:
        current().update(source=SOURCE)
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            holder[0] = conn
            try:
                if own_bracket:
                    audit_trail.record("run_start", conn=conn, object=DATASET, run_id=run_id)
                    conn.commit()
                outcome = run_files(
                    conn,
                    files,
                    raw_dir,
                    settings.processed_root,
                    force=args.force,
                    reason=args.reason,
                    run_id=run_id,
                )
                if own_bracket:
                    failed = outcome["failed"]
                    audit_trail.record(
                        "run_end",
                        conn=conn,
                        object=DATASET,
                        run_id=run_id,
                        reason=f"failed: {failed}"[:500] if failed else None,
                    )
                    conn.commit()
            finally:
                holder[0] = None
        print(
            f"done loaded={outcome['loaded']} skipped={outcome['skipped']} "
            f"failed={len(outcome['failed'])}"
        )
        return g.exit_code(1 if outcome["failed"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
