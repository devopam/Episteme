"""CLI: retire the rows of deleted ema_epar parts after a good load.

    python -m episteme.data.ema.retire [--processed-dir ...]

Thin entry point over ``episteme.data.parts_retire`` (shared with pubchem, pmc and
dailymed). ``load_ema_epar.sh`` runs it only after ``load_articles`` exited 0. With no
pending name it does nothing: no lock, no database query.
"""

from __future__ import annotations

from episteme.data import parts_retire

SOURCE = "ema_epar"


def main(argv: list[str] | None = None) -> int:
    return parts_retire.main(argv, source=SOURCE)


if __name__ == "__main__":
    raise SystemExit(main())
