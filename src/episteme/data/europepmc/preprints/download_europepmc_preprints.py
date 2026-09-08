"""Download the Europe PMC preprints feed into 01_raw/europepmc/preprints/.

STUB — real acquisition logic is ported from data/_legacy_download.py and the
existing scripts/ shell downloaders in Plan 3. See
docs/10-data-sources-runbook.md section for this feed.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "europepmc preprint download is a shell wrapper in SP3 — run "
        "scripts/data/europepmc/preprints/download_europepmc_preprint.sh "
        "(or scripts/data/run_pipeline.sh europepmc_preprint download)"
    )


if __name__ == "__main__":
    main()
