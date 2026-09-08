"""Acquisition: APOLLO — fetch curated Apollo knowledge base.

STUB — implemented in Plan 2 (extract) / Plan 3 (download). Until then use
the shell script under scripts/ (download) or the post-pull data/apollo/
extract.py entrypoint if present.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "apollo download is a shell wrapper in SP3 — run scripts/data/apollo/download_apollo.sh "
        "(or scripts/data/run_pipeline.sh apollo download)"
    )


if __name__ == "__main__":
    main()
