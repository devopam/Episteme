"""SP4 chembl serializer -- SCAFFOLD (Task 2). Task 5 fills the body."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def serialize_chembl(
    raw_dir: Path,
    processed_dir: Path,
    *,
    max_files: int = 0,
    force: bool = False,
    workers: int = 1,
    verbose: bool = False,
) -> dict:
    """Scaffold: returns an empty result. Tolerant of a missing raw dir."""
    Path(raw_dir)  # real read lands in Task 5
    return {"inputs": 0, "ok": 0, "failed": 0, "rows": 0}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m episteme.data.chembl.serialize_chembl")
    p.add_argument("--raw-dir", type=Path, default=None)
    p.add_argument("--processed-dir", type=Path, default=None)
    p.add_argument("--max-files", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--report", action="store_true")
    args = p.parse_args(argv)
    res = serialize_chembl(
        args.raw_dir or Path("01_raw/chembl"),
        args.processed_dir or Path("02_processed"),
        max_files=args.max_files,
        force=args.force,
        workers=args.workers,
        verbose=args.verbose,
    )
    print(f"chembl serialize (scaffold): {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
