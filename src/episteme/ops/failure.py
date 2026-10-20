"""Failure handoff: a stage records the unit that failed; the runner reads it.

``record_failure`` writes ``<processed root>/_ops/<source>/last_failure.json``.
``run_pipeline.sh`` clears it before a stage and, on failure, reads it with
``python -m episteme.ops.failure show`` to put the unit in the audit reason.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from episteme.config import get_settings

_ERROR_MAX = 500


def _path(source: str) -> Path:
    return get_settings().processed_root / "_ops" / source / "last_failure.json"


def record_failure(
    source: str, stage: str, unit: str, error: str, *, run_id: str | None = None
) -> Path:
    """Write the failure record atomically and return its path."""
    settings = get_settings()
    path = _path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id if run_id is not None else settings.run_id,
        "stage": stage,
        "unit": unit,
        "error": error[:_ERROR_MAX],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".last_failure.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def clear(source: str) -> None:
    _path(source).unlink(missing_ok=True)


def _show(source: str, run_id: str, field: str | None = None) -> str:
    """``unit: error``; with ``field`` only that field. Empty when there is no
    record of this run."""
    try:
        data = json.loads(_path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("run_id") != run_id:
        return ""
    if field is not None:
        return str(data.get(field) or "")
    return f"{data.get('unit', '')}: {data.get('error', '')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="episteme.ops.failure")
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show")
    show.add_argument("--source", required=True)
    show.add_argument("--run-id", required=True)
    show.add_argument("--field", choices=("unit", "error", "stage"), default=None)
    clr = sub.add_parser("clear")
    clr.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "show":
        text = _show(args.source, args.run_id, args.field)
        if text:
            print(text)
    else:
        clear(args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
