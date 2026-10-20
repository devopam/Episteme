"""Operator skip of one failed unit (operating standard section 6.2).

``--skip-unit NAME`` (repeatable) lets a stage go past a unit that fails for a
permanent reason. A skip applies to one run only; no marker survives it. Each
skipped unit gets one ``unit_skip`` audit row that holds the operator's reason.
The unit name is the name ``episteme.ops.failure.record_failure`` writes.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from episteme import audit_trail
from episteme.data.db.connection import connection


def add_skip_args(ap: argparse.ArgumentParser) -> None:
    """Add the repeatable ``--skip-unit NAME``. Add ``--reason`` only when the
    parser has none (a stage that has its own ``--reason`` shares it)."""
    ap.add_argument(
        "--skip-unit",
        action="append",
        default=None,
        metavar="NAME",
        help="skip the named unit in this run (repeatable; needs --reason; audited)",
    )
    if not any("--reason" in a.option_strings for a in ap._actions):
        ap.add_argument(
            "--reason", type=str, default=None, help="required with --skip-unit (audited)"
        )


def check_skip_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Exit 2 through ``ap.error`` when ``--skip-unit`` comes without ``--reason``."""
    if getattr(args, "skip_unit", None) and not getattr(args, "reason", None):
        ap.error("--skip-unit requires --reason")


def apply_skips(
    source: str,
    stage: str,
    unit_names: Sequence[str],
    skip_names: Sequence[str] | None,
    reason: str | None,
    *,
    run_id: str | None,
) -> list[str]:
    """Return ``unit_names`` without the skipped ones.

    A skip name that matches no unit raises ``SystemExit(2)`` before any audit
    row is written. Otherwise one ``unit_skip`` row per skipped unit is written
    and committed on a fresh connection.
    """
    if not skip_names:
        return list(unit_names)
    wanted = list(dict.fromkeys(skip_names))
    known = set(unit_names)
    unknown = [n for n in wanted if n not in known]
    if unknown:
        print(
            f"error: --skip-unit name(s) {unknown!r} match no {stage} unit of {source}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    with connection() as conn:
        with audit_trail.deferred_mirror(conn):
            try:
                for name in wanted:
                    audit_trail.record(
                        "unit_skip",
                        conn=conn,
                        object=f"{source} {name}",
                        reason=reason,
                        run_id=run_id,
                    )
                audit_trail.commit(conn)
            except BaseException:
                audit_trail.rollback(conn)
                raise
    skipped = set(wanted)
    return [n for n in unit_names if n not in skipped]
