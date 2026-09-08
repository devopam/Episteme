"""Download the Europe PMC id_mappings feed into 01_raw/europepmc/id_mappings/.

STUB — real acquisition logic is ported from data/_legacy_download.py and the
existing scripts/ shell downloaders in Plan 3. See
docs/10-data-sources-runbook.md section for this feed.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "europepmc id_mappings download is a shell wrapper in SP3 — run "
        "scripts/data/europepmc/id_mappings/download_europepmc_id_mappings.sh "
        "(or scripts/data/run_pipeline.sh europepmc_id_mappings download)"
    )


if __name__ == "__main__":
    main()
