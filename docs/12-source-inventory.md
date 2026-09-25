# Episteme — Source Inventory

**Status:** Living document (SP5). Pinned to the `WRAPPER` table in `scripts/data/run_pipeline.sh`; `tests/docs/test_source_inventory_doc.py` fails if a wired source is missing here or a script path drifts.

**Purpose:** One row per wired source: class, which pipeline stages exist, licence class and its basis, upstream cadence, and the download script. These are the *static* columns. Machine-local columns (last sync, row count) are produced per machine by `scripts/data/source_inventory.sh` and are not recorded here.

Conventions: "not stated" means neither the code nor `docs/02` / `docs/10` states a value; it is not a claim that none exists. Licence cells describe what the code records (the `license` / `subset` an extractor or serializer assigns), not a legal determination.

| source | class | stages wired | licence (class + basis) | cadence | script |
|---|---|---|---|---|---|
| `pmc` | literature | download, extract, load, graph, materialize, enrich | per-article, from PMC metadata `license_code`, normalised by `article_schema.normalize_license`; `commercial` only for CC0 / CC BY / BY-SA / BY-ND, otherwise `text_mining` (`extract_pmc.py`) | continuous / daily (docs/10 s2) | `pmc/download_pmc.sh` |
| `pubmed` | literature | download, extract, load, graph | `unknown` -> `open_metadata`: records carry no licence text (`extract_pubmed.py`) | baseline annual; update files daily (docs/10 s1) | `pubmed/download_pubmed.sh` |
| `apollo` | literature | download, extract, load, graph | `permissive` -> `commercial`, from the HF card's `apache-2.0` (`extract_apollo.py`); docs/02 still lists commercial-use diligence as open | static corpus releases (docs/10 s8) | `apollo/download_apollo.sh` |
| `europepmc_preprint` | literature | download, extract, load, graph | per-article licence from JATS, normalised; NC licences -> `text_mining` (docs/10 s3) | incremental range files as EPMC publishes (docs/10 s3) | `europepmc/preprints/download_europepmc_preprint.sh` |
| `europepmc_manuscript` | literature | download, extract, load, graph | `text_mining` hardcoded subset (`extract_europepmc_manuscripts.py`; docs/10 s6) | baseline periodic; incrementals daily when published (docs/10 s6) | `europepmc/manuscripts/download_europepmc_manuscript.sh` |
| `europepmc_id_mappings` | EPMC support feed | download, load (to `episteme.id_map`) | not stated (docs/02 lists "Open"); no corpus rows | monthly, file overwritten on the 1st (docs/10 s5) | `europepmc/id_mappings/download_europepmc_id_mappings.sh` |
| `europepmc_lite` | EPMC support feed | download, enrich | not stated; enrichment only, no corpus rows | weekly (docs/10 s7) | `europepmc/lite_metadata/download_europepmc_lite.sh` |
| `europepmc_abstracts` | literature (download-only) | download | not stated | "monthly-style packages when published" (docs/10 s4) | `europepmc/abstracts/download_europepmc_abstracts.sh` |
| `bookshelf` | literature | download, extract, load, graph | per-book, from the book XML licence / copyright text via `normalize_license`; parts inherit the book's licence (`extract_bookshelf.py`) | not stated | `bookshelf/download_bookshelf.sh` |
| `guidelines` | literature | download, extract, load, graph | `unknown`, `subset=other` hardcoded: HF card is `license: other` with mixed per-issuing-body terms; no single licence (`extract_guidelines.py`) | not stated | `guidelines/download_guidelines.sh` |
| `chembl` | structured | download, serialize, load | `CC BY-SA` -> `commercial`; ChEMBL's stated release licence CC BY-SA 3.0 (`serialize_chembl.py`) | periodic dumps (docs/02) | `chembl/download_chembl.sh` |
| `uniprot` | structured | download, serialize, load | real licence CC BY 4.0, but `subset=text_mining` is a hardcoded governance override (`serialize_uniprot.py`) | about 8-week releases (docs/02) | `uniprot/download_uniprot.sh` |
| `pubchem` | structured | download, serialize, load | `public_domain` **governance override** (decision 2026-09-19): `normalize_license` returns `unknown` for the Fair Use text; PubChem carries submitter-contributed content risk (`serialize_pubchem.py`) | frequent (docs/02) | `pubchem/download_pubchem.sh` |
| `clinvar` | structured | download, serialize, load | `public_domain` **governance override** (decision 2026-09-19): `normalize_license` returns `unknown` for ClinVar's data-use policy; carries submitter-contributed content risk (`serialize_clinvar.py`) | "regular" (docs/02) | `clinvar/download_clinvar.sh` |
| `reactome` | structured | download, serialize, load | `CC0` -> `commercial`, from Reactome's License Agreement s1c (`serialize_reactome.py`) | not stated | `reactome/download_reactome.sh` |
| `mesh` | structured | download, serialize, load, graph | `public_domain` **governance override** (decision 2026-09-19): `normalize_license` returns `unknown` for NLM's MeSH terms (`serialize_mesh.py`) | not stated | `mesh/download_mesh.sh` |
| `ontologies` | structured | download, serialize, load | per ontology, from the declared OBO licence: GO and MONDO `CC BY` -> `commercial`; HPO `unknown` -> `open_metadata`; UCUM not checked or serialized (`serialize_ontologies.py`) | not stated | `ontologies/download_ontologies.sh` |
| `openalex` | structured | download, serialize, load | `CC0` -> `commercial`, the dataset-level metadata licence, not per-work OA licences (`serialize_openalex.py`) | not stated | `openalex/download_openalex.sh` |
| `hf_corpus` | acquisition mechanism | download | depends on the repo id passed; not stated | not stated | `hf_corpus/download_hf_corpus.sh` |
| `dailymed` | volatile (Stream 2) | download | not stated | daily / weekly / monthly zips (docs/02 s2.2) | `dailymed/download_dailymed.sh` |
| `openfda` | volatile (Stream 2) | download | not stated | API / bulk (docs/02 s2.2) | `openfda/download_openfda.sh` |
| `aact` | volatile (Stream 2) | download | not stated | monthly / daily dumps (docs/02 s2.2) | `aact/download_aact.sh` |
| `cdisc_bc` | volatile (Stream 2) | download (no serializer) | **UNVERIFIED**: the repo README says code is MIT and only docs/minutes are CC-BY-4.0; nothing is stated for the `export/` data files (`download_cdisc_bc.sh` header); until CDISC confirms, this source stays out of any training corpus | not stated (pinned per run to one commit) | `cdisc_bc/download_cdisc_bc.sh` |

Notes on the licence column: `public_domain` never comes from `normalize_license()`; only serializers set it, as an explicit governance override, and `license_raw` keeps the real upstream text. `subset_from_license` maps it to `commercial`.

## How to refresh machine-local columns

Last sync and row counts depend on the machine's `01_raw` tree and database, so they are not stored here. Run `scripts/data/source_inventory.sh` on the machine in question; it reports the machine-local columns for the sources above (`last_sync` is the newest `last_sync_utc.txt` found anywhere under the source's raw directory, since some downloaders write the stamp in a nested folder). This file changes only when the wired-source set, a script path, a licence ruling or a cadence changes.

## Class definitions

From the roadmap spec s1 (`docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md`):

- **Literature:** `extract_` -> `episteme.articles` -> graph (`article_cites`, `article_mesh`) + `03_corpus`.
- **EPMC support feeds:** download plus load or enrich; no corpus rows.
- **Stable structured:** `serialize_` -> declarative-prose rows -> `episteme.articles` -> `03_corpus`; no graph, except `mesh` descriptors feeding `article_mesh`.
- **Volatile (Stream 2 / Phase 1):** download to `01_raw/<source>/` plus provenance only; no `serialize_`, no `articles` rows this phase.
- **Acquisition mechanism:** `hf_corpus`, a generic `<repo_id>` Hugging Face wrapper.
