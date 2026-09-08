"""Download the Europe PMC lite_metadata feed into 01_raw/europepmc/lite_metadata/.

STUB — real acquisition logic is ported from data/_legacy_download.py and the
existing scripts/ shell downloaders in Plan 3. See
docs/10-data-sources-runbook.md section for this feed.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "europepmc lite download is a shell wrapper in SP3 — run "
        "scripts/data/europepmc/lite_metadata/download_europepmc_lite.sh "
        "(or scripts/data/run_pipeline.sh europepmc_lite download)"
    )


if __name__ == "__main__":
    main()
