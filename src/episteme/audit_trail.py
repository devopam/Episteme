"""ALCOA+ tamper-evident audit trail.

``record()`` writes one ``episteme._audit`` row **in the caller's transaction**
(it never commits) and appends the same record to an append-only JSONL mirror
under ``<processed_root>/_ops/_audit/``. Every row is SHA-256 hash-chained:
``record_hash`` is the digest of the canonical JSON of every inserted field
except ``seq``, ``recorded_at`` and ``record_hash`` itself -- ``prev_hash``
(the previous row's ``record_hash``) is one of the hashed fields, so any edit
to any historical row breaks the chain from that point forward.

``verify()`` walks the table by ``seq``, recomputes each ``record_hash``,
checks every ``prev_hash`` links to the previous stored ``record_hash``, and
cross-checks the JSONL mirror line count against the table row count.

Config comes from ``episteme.config`` (``require_actor`` / ``get_settings``) --
this module never reads ``os.environ`` directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket

# subprocess is used only for a fixed `git rev-parse` arg list, no shell.
import subprocess  # nosec B404
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from episteme.config import get_settings, require_actor

_LOG = logging.getLogger(__name__)

# Frozen event-type set (phase-0 data roadmap Sec 4.8). The roadmap text is the
# tiebreaker over any other count: it lists exactly these 13 names. record()
# rejects anything else with ValueError *before* any config or DB work.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_start",
        "run_end",
        "extract_commit",
        "serialize_commit",
        "load_commit",
        "load_replace",
        "graph_commit",
        "corpus_materialize",
        "schema_migration",
        "force_override",
        "integrity_check",
        "manual_correction",
        "config_change",
    }
)

# Exactly the fields that go into the hash, in a single place so record() and
# verify() cannot drift. Excludes seq + recorded_at (DB-assigned, unknown
# pre-insert) and record_hash (the output). prev_hash IS included -> the chain.
_HASHED_FIELDS: tuple[str, ...] = (
    "actor",
    "host",
    "pid",
    "run_id",
    "code_version",
    "event_type",
    "object",
    "input_content_hash",
    "rows_affected",
    "old_value",
    "new_value",
    "reason",
    "prev_hash",
)

_GENESIS_HASH = "0" * 64


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, non-JSON types via str()."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _record_hash(fields: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the hashed field subset.

    Note: hash equality across the DB round-trip is guaranteed for dicts of
    str/int/bool/None inside old_value/new_value. It can break for float /
    Decimal payloads because Postgres normalises jsonb numerics (e.g. 1e10 ->
    10000000000). Out of scope for this task; revisit if a caller ever passes
    numeric jsonb payloads.
    """
    subset = {k: fields[k] for k in _HASHED_FIELDS}
    return hashlib.sha256(_canonical_json(subset).encode("utf-8")).hexdigest()


def _code_version() -> str:
    """`git rev-parse --short HEAD`, best-effort -> "unknown"."""
    git = shutil.which("git")
    if not git:
        return "unknown"
    try:
        # Absolute path from shutil.which, fixed arg list, no shell.
        proc = subprocess.run(  # nosec B603
            [git, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def _mirror_dir():
    return get_settings().processed_root / "_ops" / "_audit"


def _mirror_path():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _mirror_dir() / f"audit-{stamp}.jsonl"


def record(
    event_type: str,
    *,
    conn: psycopg.Connection,
    object: str | None = None,  # noqa: A002 - name fixed by the frozen public signature
    input_content_hash: str | None = None,
    rows_affected: int | None = None,
    old_value: Any = None,
    new_value: Any = None,
    reason: str | None = None,
    run_id: str | None = None,
) -> str:
    """Insert one hash-chained ``episteme._audit`` row in ``conn``'s transaction.

    Also appends the full record (including ``record_hash``) as one line to the
    day's JSONL mirror. Returns the ``record_hash``. Does NOT commit -- the
    caller owns the transaction.
    """
    # Closed-set check first: before require_actor(), before get_settings(),
    # before touching the cursor -- test_bad_event_type_rejected relies on this.
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"unknown audit event_type {event_type!r}; must be one of {sorted(EVENT_TYPES)}"
        )
    # Same ordering discipline as the EVENT_TYPES check above (before
    # require_actor()/get_settings()/the cursor): manual_correction and
    # schema_migration must always carry a human-readable reason.
    if event_type in ("manual_correction", "schema_migration") and not reason:
        raise ValueError(f"{event_type!r} requires a non-empty reason")

    actor = require_actor()
    host = socket.gethostname()
    pid = os.getpid()
    code_version = _code_version()

    with conn.cursor() as cur:
        # Transaction-scoped advisory lock: serializes every record() call across
        # every process (not just threads in one process) so two concurrent
        # callers can never read the same prev_hash and fork the chain. Released
        # automatically at COMMIT/ROLLBACK of conn's transaction.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('episteme._audit'))")
        cur.execute("SELECT record_hash FROM episteme._audit ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
    prev_hash = row[0] if row else _GENESIS_HASH

    fields: dict[str, Any] = {
        "actor": actor,
        "host": host,
        "pid": pid,
        "run_id": run_id,
        "code_version": code_version,
        "event_type": event_type,
        "object": object,
        "input_content_hash": input_content_hash,
        "rows_affected": rows_affected,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
        "prev_hash": prev_hash,
    }
    rec_hash = _record_hash(fields)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episteme._audit (
                actor, host, pid, run_id, code_version, event_type, object,
                input_content_hash, rows_affected, old_value, new_value, reason,
                prev_hash, record_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                actor,
                host,
                pid,
                run_id,
                code_version,
                event_type,
                object,
                input_content_hash,
                rows_affected,
                Jsonb(old_value) if old_value is not None else None,
                Jsonb(new_value) if new_value is not None else None,
                reason,
                prev_hash,
                rec_hash,
            ),
        )

    # Mirror AFTER the successful INSERT. A failed mirror write must not roll
    # back the DB row (the caller's txn is untouched here) -- but it DOES
    # propagate so a broken mirror is loud rather than silent.
    mirror_record = dict(fields)
    mirror_record["record_hash"] = rec_hash
    mirror_record["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        _mirror_dir().mkdir(parents=True, exist_ok=True)
        with open(_mirror_path(), "a", encoding="utf-8") as fh:
            fh.write(_canonical_json(mirror_record) + "\n")
    except OSError:
        # Resolution 6: a broken mirror must be loud, not silent. The DB row is
        # already inserted in the caller's txn and stays; we log and re-raise so
        # the caller sees the mirror is out of sync.
        _LOG.exception("audit mirror write failed for record_hash=%s", rec_hash)
        raise

    return rec_hash


def verify(conn: psycopg.Connection) -> list[dict]:
    """Walk ``episteme._audit`` by ``seq`` and return a list of problems.

    Empty list == chain intact. Each problem is a dict:
      - ``{"seq": n, "reason": "hash_mismatch"}`` -- stored record_hash != recompute
      - ``{"seq": n, "reason": "chain_break"}``   -- prev_hash != prior stored record_hash
      - ``{"seq": None, "reason": "mirror_short", "mirror": x, "table": y}``
    """
    problems: list[dict] = []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT seq, actor, host, pid, run_id, code_version, event_type,
                   object, input_content_hash, rows_affected, old_value,
                   new_value, reason, prev_hash, record_hash
            FROM episteme._audit
            ORDER BY seq
            """
        )
        rows = cur.fetchall()

    cols = (
        "seq",
        "actor",
        "host",
        "pid",
        "run_id",
        "code_version",
        "event_type",
        "object",
        "input_content_hash",
        "rows_affected",
        "old_value",
        "new_value",
        "reason",
        "prev_hash",
        "record_hash",
    )

    expected_prev = _GENESIS_HASH
    for row in rows:
        rec = dict(zip(cols, row, strict=True))
        seq = rec["seq"]
        if _record_hash(rec) != rec["record_hash"]:
            problems.append({"seq": seq, "reason": "hash_mismatch"})
        if rec["prev_hash"] != expected_prev:
            problems.append({"seq": seq, "reason": "chain_break"})
        expected_prev = rec["record_hash"]

    # Mirror parity: total non-blank lines across every audit-*.jsonl vs table
    # row count. One-sided -- mirror > table is legitimate (e.g. after a schema
    # recreate that truncates the table but leaves the mirror files).
    mirror_lines = 0
    mdir = _mirror_dir()
    if mdir.is_dir():
        for path in mdir.glob("audit-*.jsonl"):
            with open(path, encoding="utf-8") as fh:
                mirror_lines += sum(1 for line in fh if line.strip())
    if mirror_lines < len(rows):
        problems.append(
            {
                "seq": None,
                "reason": "mirror_short",
                "mirror": mirror_lines,
                "table": len(rows),
            }
        )

    return problems


def mirror_only(event_type: str, **fields: Any) -> None:
    """Best-effort, JSONL-only audit record: NO DB write, NO hash chain.

    Best-effort fallback for callers whose ``record()`` attempt failed (DB
    unreachable, refused by the DB-mode guard, or the insert failed) -- e.g. the
    extract/serialize stages' ``_best_effort_audit``, which normally calls
    ``record()`` on a fresh connection and only falls back here.
    Same closed-set ``event_type`` check as
    ``record()`` (raises ``ValueError`` before anything else). Unlike
    ``record()``, a failure to write the mirror file is logged via
    ``_LOG.exception`` and swallowed rather than re-raised -- this is already
    the last-resort fallback, so there is nowhere further for it to escalate
    to; ``record()``'s own mirror failure stays loud per its docstring.

    The written line carries ``event_type``, a UTC ISO-8601 ``ts``, an
    explicit ``"chained": False`` (so a reader can tell it apart from a real
    hash-chained ``record()`` mirror line), and every kwarg the caller passed
    -- all run through the same ``_canonical_json`` the rest of this module
    uses.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"unknown audit event_type {event_type!r}; must be one of {sorted(EVENT_TYPES)}"
        )

    mirror_record: dict[str, Any] = {
        "event_type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "chained": False,
        **fields,
    }
    try:
        _mirror_dir().mkdir(parents=True, exist_ok=True)
        with open(_mirror_path(), "a", encoding="utf-8") as fh:
            fh.write(_canonical_json(mirror_record) + "\n")
    except OSError:
        _LOG.exception("mirror_only write failed for event_type=%s", event_type)


def main(argv: list[str] | None = None) -> int:
    """python -m episteme.audit_trail record EVENT_TYPE [--object O] [--run-id R] [--reason X]

    Opens its own connection, calls record(), commits, prints the record_hash.
    """
    import argparse
    import sys

    from episteme.data.db.connection import connection

    p = argparse.ArgumentParser(prog="python -m episteme.audit_trail")
    sub = p.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("event_type")
    rec.add_argument("--object", dest="object_", default=None)
    rec.add_argument("--run-id", default=None)
    rec.add_argument("--reason", default=None)
    args = p.parse_args(argv)

    if args.cmd == "record":
        try:
            with connection() as conn:
                h = record(
                    args.event_type,
                    conn=conn,
                    object=args.object_,
                    run_id=args.run_id,
                    reason=args.reason,
                )
                conn.commit()
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(h)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
