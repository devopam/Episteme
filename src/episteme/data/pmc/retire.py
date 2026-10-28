"""CLI: retire the rows of deleted pmc parts after a good load.

    python -m episteme.data.pmc.retire [--processed-dir ...]

Thin entry point over ``episteme.data.parts_retire`` (shared with pubchem).
``load_pmc.sh`` runs it only after ``load_articles`` exited 0. With no
``_ops/pmc/retire_pending.json`` (or an empty list) it does nothing: no lock,
no database query.
"""

from __future__ import annotations

from episteme.data import parts_retire

SOURCE = "pmc"


def main(argv: list[str] | None = None) -> int:
    return parts_retire.main(argv, source=SOURCE)


if __name__ == "__main__":
    raise SystemExit(main())
