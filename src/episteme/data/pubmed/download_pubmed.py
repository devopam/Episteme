"""Acquisition: PUBMED — download baseline XML files.

STUB — implemented in Plan 2 (extract) / Plan 3 (download). Until then use
the shell script under scripts/ (download) or the post-pull data/pubmed/
extract.py entrypoint if present.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "pubmed download is a shell wrapper in SP3 — run scripts/data/pubmed/download_pubmed.sh "
        "(or scripts/data/run_pipeline.sh pubmed download)"
    )


if __name__ == "__main__":
    main()
