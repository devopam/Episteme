"""Load reference code lists into ``episteme.code_system`` and ``episteme.code``.

A missing optional list is skipped. A file that is present but not valid fails
that list. An empty licence notice never reaches the database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from episteme.audit_trail import record
from episteme.config import get_settings
from episteme.data.checkpoint_markers import (
    input_key,
    load_success_marker_path,
    write_marker,
)
from episteme.data.db.connection import connection
from episteme.data.refmeta.codelists import parse_codelist_dir
from episteme.data.refmeta.edqm import parse_edqm_dir
from episteme.data.refmeta.iso639 import parse_iso639
from episteme.data.refmeta.iso3166 import parse_iso3166
from episteme.data.refmeta.loinc import parse_loinc_zip
from episteme.data.refmeta.model import CodeRelease
from episteme.data.refmeta.oids import parse_oid_pdf
from episteme.data.refmeta.ucum import parse_ucum

_LOG = logging.getLogger("episteme.refmeta")

SYSTEMS = ("e2b_oid", "e2b_cl", "ucum", "iso639", "iso3166", "loinc", "edqm")

_COPY = """
COPY episteme.code (
    system_key, release_id, code, display, status, extra, source_file
) FROM STDIN
"""
_TYPES = ("text", "text", "text", "text", "text", "jsonb", "text")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load reference code lists")
    parser.add_argument("--system", action="append", choices=SYSTEMS)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--processed-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.force and not (args.reason and args.reason.strip()):
        print("refmeta load: --force needs --reason", file=sys.stderr)
        return 2
    settings = get_settings()
    raw = args.raw_dir or settings.raw_root
    processed = args.processed_dir or settings.processed_root
    wanted = tuple(args.system or SYSTEMS)
    try:
        releases = collect(raw, wanted, required=bool(args.system))
    except ValueError as exc:
        print(f"refmeta load: {exc}", file=sys.stderr)
        return 1
    if not releases:
        _LOG.info("refmeta: nothing to load")
        return 0
    try:
        with connection() as conn:
            for release in releases:
                status = load_release(
                    conn,
                    release,
                    processed_root=processed,
                    force=args.force,
                    reason=args.reason,
                )
                _LOG.info(
                    "%s %s: %s (%s codes)",
                    release.system_key,
                    release.release_id,
                    status,
                    len(release.codes),
                )
    except psycopg.errors.UndefinedTable:
        print("refmeta load: apply migration 0013_reference_codes.sql first", file=sys.stderr)
        return 1
    return 0


def collect(raw: Path, systems: tuple[str, ...], *, required: bool) -> list[CodeRelease]:
    releases: list[CodeRelease] = []
    missing: list[str] = []
    for system in systems:
        release = _read_system(raw, system)
        if release is None:
            missing.append(system)
            continue
        releases.append(release)
    if required and missing:
        raise ValueError(f"no files for {', '.join(missing)}")
    for system in missing:
        _LOG.info("%s: no files; skipped", system)
    return releases


def load_release(
    conn: psycopg.Connection,
    release: CodeRelease,
    *,
    processed_root: Path,
    force: bool,
    reason: str | None,
) -> str:
    marker = load_success_marker_path(
        processed_root, "refmeta", f"{release.system_key}__{release.release_id}"
    )
    database = _db_identity(conn)
    digest = release_digest(release)
    if not force and marker_counts(
        marker, database=database, source_file=release.source_file, digest=digest
    ):
        return "skipped"
    with conn.cursor() as cur:
        cur.execute("LOCK TABLE episteme.code_system IN EXCLUSIVE MODE")
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM episteme.code_system WHERE system_key = %s)",
            (release.system_key,),
        )
        replaced = bool(cur.fetchone()[0])
        cur.execute(
            "DELETE FROM episteme.code WHERE system_key = %s AND release_id = %s",
            (release.system_key, release.release_id),
        )
        cur.execute(
            "DELETE FROM episteme.code_system WHERE system_key = %s AND release_id = %s",
            (release.system_key, release.release_id),
        )
        cur.execute(
            "UPDATE episteme.code_system SET is_latest = false WHERE system_key = %s AND is_latest",
            (release.system_key,),
        )
        cur.execute(
            """
            INSERT INTO episteme.code_system (
                system_key, release_id, version, oid, licence_notice, source_file, is_latest
            ) VALUES (%s, %s, %s, %s, %s, %s, true)
            """,
            (
                release.system_key,
                release.release_id,
                release.version,
                release.oid,
                release.licence_notice,
                release.source_file,
            ),
        )
        with cur.copy(_COPY) as copy:
            copy.set_types(_TYPES)
            for row in release.codes:
                copy.write_row(
                    (
                        release.system_key,
                        release.release_id,
                        row.code,
                        row.display,
                        row.status,
                        Jsonb(row.extra),
                        release.source_file,
                    )
                )
    record(
        "load_replace" if replaced else "load_commit",
        conn=conn,
        object=f"episteme.code:{release.system_key}:{release.release_id}",
        rows_affected=len(release.codes),
        reason=reason,
        new_value={
            "system_key": release.system_key,
            "release_id": release.release_id,
            "rows": len(release.codes),
            "source_file": release.source_file,
        },
    )
    conn.commit()
    write_marker(
        marker,
        {
            "source": "refmeta",
            "system_key": release.system_key,
            "release_id": release.release_id,
            "rows": len(release.codes),
            "source_file": release.source_file,
            "database": database,
            "digest": digest,
            "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    return "replaced" if replaced else "loaded"


def release_digest(release: CodeRelease) -> str:
    """SHA-256 of what a load writes (header and every code row), so a changed file
    under the same release folder is loaded again."""
    h = hashlib.sha256()
    head = [
        release.system_key,
        release.release_id,
        release.version,
        release.oid,
        release.licence_notice,
        release.source_file,
    ]
    h.update(json.dumps(head, ensure_ascii=False).encode())
    for row in release.codes:
        item = [row.code, row.display, row.status, row.extra]
        h.update(json.dumps(item, ensure_ascii=False, sort_keys=True).encode())
        h.update(b"\n")
    return h.hexdigest()


def marker_counts(marker: Path, *, database: str, source_file: str, digest: str) -> bool:
    """A load marker skips a release only when it was written for the same target
    database, the same input (``input_key``) and the same content. A load into
    episteme_test must not make the production load skip, and a replaced file in
    the same release folder must load again."""
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        data.get("database") == database
        and data.get("source_file") == source_file
        and data.get("digest") == digest
    )


def _db_identity(conn: psycopg.Connection) -> str:
    """Name, server address and port of the target database (no credentials)."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), inet_server_addr()::text, inet_server_port()")
        row = cur.fetchone()
    return "|".join(str(v) for v in row) if row else "unknown"


def _read_system(raw: Path, system: str) -> CodeRelease | None:
    if system == "e2b_oid":
        return _read_oid(raw)
    if system == "e2b_cl":
        return _read_codelists(raw)
    folder = _newest(raw / "refmeta" / system)
    if folder is None:
        return None
    notice = _notice(folder)
    if system == "ucum":
        path = _one_file(folder, "ucum-essence.xml")
        return parse_ucum(path, notice=notice, source_file=_key(path, raw), release_id=folder.name)
    if system == "iso639":
        path = _one_file(folder, "ISO-639-2_utf-8.txt")
        return parse_iso639(
            path.read_text(encoding="utf-8"),
            notice=notice,
            source_file=_key(path, raw),
            release_id=folder.name,
        )
    if system == "iso3166":
        path = _one_file(folder, "codes.csv")
        return parse_iso3166(
            path.read_text(encoding="utf-8"),
            notice=notice,
            source_file=_key(path, raw),
            release_id=folder.name,
        )
    if system == "loinc":
        path = _one_suffix(folder, ".zip")
        return parse_loinc_zip(
            path, notice=notice, source_file=_key(path, raw), release_id=folder.name
        )
    if system == "edqm":
        return parse_edqm_dir(
            folder, notice=notice, source_file=_key(folder, raw), release_id=folder.name
        )
    raise ValueError(f"unknown system {system}")


def _read_oid(raw: Path) -> CodeRelease | None:
    package = raw / "ich" / "e2" / "ig_v1_11"
    if not package.is_dir():
        return None
    pdfs = sorted(package.glob("*Implementation_Guide*.pdf"))
    if not pdfs:
        return None
    path = next((p for p in pdfs if "v5_03" in p.name), pdfs[0])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return parse_oid_pdf(path, source_file=_key(path, raw), release_id=f"ig-v5.03-{digest}")


def _read_codelists(raw: Path) -> CodeRelease | None:
    package = raw / "ich" / "e2" / "ig_v1_11"
    if not package.is_dir():
        return None
    folders = sorted(p for p in package.glob("*Code Lists*") if p.is_dir())
    if not folders:
        return None
    return parse_codelist_dir(folders[0], source_file=_key(folders[0], raw))


def _newest(folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    children = [p for p in folder.iterdir() if p.is_dir()]
    if not children:
        return None
    return max(children, key=lambda p: p.stat().st_mtime)


def _notice(folder: Path) -> str:
    for name in ("NOTICE.txt", "CONDITIONS.txt"):
        path = folder / name
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
    raise ValueError(f"{folder}: NOTICE.txt or CONDITIONS.txt is missing or empty")


def _one_file(folder: Path, name: str) -> Path:
    path = folder / name
    if not path.is_file():
        raise ValueError(f"{folder}: {name} is missing")
    return path


def _one_suffix(folder: Path, suffix: str) -> Path:
    found = sorted(p for p in folder.glob(f"*{suffix}") if p.is_file())
    if len(found) != 1:
        raise ValueError(f"{folder}: expected one {suffix} file, found {len(found)}")
    return found[0]


def _key(path: Path, raw: Path) -> str:
    return input_key(path, raw)


if __name__ == "__main__":
    raise SystemExit(main())
