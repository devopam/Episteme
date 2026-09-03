"""Episteme data pipeline: acquisition, extraction, loading, curation.

Source-folder <-> wire-name map (article_schema.SOURCES):
    pubmed/                  -> pubmed
    pmc/                     -> pmc
    europepmc/preprints/     -> europepmc_preprint   (Plan 2 aligns the wire values)
    europepmc/manuscripts/   -> europepmc_manuscript
    europepmc/lite_metadata/ -> europepmc_lite
    apollo/                  -> apollo
"""
