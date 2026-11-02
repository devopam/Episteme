"""CLI: retire the rows of deleted dailymed parts after a good load.

    python -m episteme.data.dailymed.retire [--processed-dir ...]

Thin entry point over ``episteme.data.parts_retire`` (shared with pubchem and pmc).
``load_dailymed.sh`` runs it only after ``load_articles`` exited 0. Buckets add their
pending names to ``_ops/dailymed/retire_pending.d/<bucket key>.json``. The delete of a
part's articles also deletes its label rows (``postgres_loader.delete_source_files``).
With no pending name it does nothing: no lock, no database query.
"""

from __future__ import annotations

from episteme.data import parts_retire

SOURCE = "dailymed"


def main(argv: list[str] | None = None) -> int:
    return parts_retire.main(argv, source=SOURCE)


if __name__ == "__main__":
    raise SystemExit(main())
