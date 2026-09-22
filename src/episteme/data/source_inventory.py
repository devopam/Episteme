"""Read-only source inventory: machine-local last-sync and row counts.

Never writes to disk or the database. Degrades to ``n/a`` when the database
is unreachable. The raw root comes from settings (config.py is the sole
environment reader).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from episteme.config import get_settings

_REPO = Path(__file__).resolve().parents[3]
_RUN = _REPO / "scripts" / "data" / "run_pipeline.sh"
_STAMP = "last_sync_utc.txt"


def wrapper_sources(run_pipeline: Path = _RUN) -> list[str]:
    text = run_pipeline.read_text(encoding="utf-8")
    block = re.search(r"declare -A WRAPPER=\((.*?)\n\)", text, re.S)
    if not block:
        return []
    return re.findall(r'\[(\w+)\]="[^"]+"', block.group(1))


def _newest_stamp(source_dir: Path) -> str:
    """Newest stamp anywhere under ``source_dir`` (ISO-8601 UTC sorts as text)."""
    newest = ""
    try:
        for stamp in source_dir.rglob(_STAMP):
            try:
                value = stamp.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            newest = max(newest, value)
    except OSError:
        return ""
    return newest


def collect(raw_root: Path, sources: list[str], counts: dict[str, int] | None) -> list[dict]:
    rows = []
    for s in sources:
        rows.append(
            {
                "source": s,
                "last_sync": _newest_stamp(Path(raw_root) / s),
                "rows": None if counts is None else counts.get(s),
            }
        )
    return rows


def _db_counts() -> dict[str, int] | None:
    try:
        from episteme.data.db.connection import connection

        with connection() as c:
            cur = c.execute("select source, count(*) from episteme.articles group by 1")
            return {str(r[0]): int(r[1]) for r in cur.fetchall()}
    except Exception:
        return None


def _fmt(rows: list[dict]) -> str:
    cells = [
        (r["source"], r["last_sync"] or "n/a", "n/a" if r["rows"] is None else str(r["rows"]))
        for r in rows
    ]
    head = ("source", "last_sync", "rows")
    w = [max(len(x[i]) for x in [head, *cells]) for i in range(3)]
    return "\n".join(
        "  ".join(v.ljust(w[i]) for i, v in enumerate(line)).rstrip() for line in [head, *cells]
    )


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    rows = collect(Path(settings.raw_root), wrapper_sources(), _db_counts())
    print(_fmt(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
