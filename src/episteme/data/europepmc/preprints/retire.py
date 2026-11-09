"""CLI: retire the rows of deleted europepmc_preprint parts after a good load.

    python -m episteme.data.europepmc.preprints.retire [--processed-dir ...]

Thin entry point over ``episteme.data.parts_retire`` (shared with pmc and pubchem).
``load_europepmc_preprint.sh`` runs it only after ``load_articles`` exited 0.
"""

from __future__ import annotations

from episteme.data import parts_retire

SOURCE = "europepmc_preprint"


def main(argv: list[str] | None = None) -> int:
    return parts_retire.main(argv, source=SOURCE)


if __name__ == "__main__":
    raise SystemExit(main())
