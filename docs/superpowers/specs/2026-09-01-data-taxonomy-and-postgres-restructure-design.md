# Design — Data-taxonomy restructure + Postgres hybrid storage

**Date:** 2026-09-01
**Branch:** `refactor/data-taxonomy-and-postgres`
**Status:** Approved design — ready for implementation planning
**Supersedes storage decisions in:** `docs/07-knowledge-graph-lessons.md`, `docs/08-data-storage-principles.md`, `docs/09-extraction-contract.md` (all to be rewritten as part of this work)

---

## 1. Goal

Reorganise `src/` and `scripts/` into a uniform, subject-area taxonomy with spelled-out
nomenclature; finish the half-done `data_pipeline/ → data/` migration that arrived on `main`;
replace the (unimplemented) Iceberg + filesystem-catalog storage target with a **Postgres
hybrid**; scaffold every acquisition source and stage — including deferred ones — up front so
later refactors are moves, not restructures; pull all environment-varying configuration
out of code into a single `.env` + `config.py`; and stand up an **ALCOA+-aligned,
tamper-evident audit trail** so the provenance of this medical/pharma-adjacent data is
defensible in a later GxP context.

Optimise for **low rework later**. Ceremony now buys correctness during the significant data
+ model work that follows.

### Non-goals

- Implementing the full RAG / Stream 2 retrieval layer (only its storage substrate is scaffolded).
- Cross-source deduplication into a single "best" article.
- A production OpenMetadata deployment (the enrich step emits a manifest; ingest is opt-in).
- Rewriting extractor parsing logic beyond what the taxonomy move and the Postgres target require.
- Model-training logic changes (only the module move + rename + import fixes).

---

## 2. Current state (post-pull, commit `a6f10cd`)

- **Two parallel `src` trees.** New `src/episteme/data/{apollo,epmc,pmc,pubmed}/` (extract-only,
  from upstream) sits next to the old `src/episteme/data_pipeline/` (download, dedup,
  decontaminate, preprocess) and `src/episteme/model_pipeline/` (untouched).
- **`scripts/` is flat and inconsistent** — `extract_pubmed.sh` is a 90-line path-hunter,
  `extract_pmc_oa_comm.sh` is a 30-line wrapper, `extract_apollo_corpus.sh` still calls the
  pre-rewrite Apollo interface (`--input-dir/--output-jsonl`) and is broken.
- **Docs 09/10 already describe the target** (`scripts/data/extract_<source>.sh`, per-source
  `src/episteme/data/<source>/extract.py`, Iceberg + OpenMetadata) but code has not moved there,
  and the Iceberg/OpenMetadata steps do not exist — `writer.py` writes plain partitioned Parquet.
- **`analyze_source_samples.py`** referenced by docs 09/10 does not exist.
- **Tests are red at collection**: the upstream rewrite of `scripts/download_pmc_oa_comm.py`
  removed `query_esearch` / `get_metadata` / `download_pmc_commercial`, so
  `tests/test_pmc_download.py` fails to import and halts the whole run.
- **`pyproject.toml`** exists (setuptools, v0.1.0, one flat dependency list); no tooling config.
- **`LICENSE`** file is MIT; `pyproject.toml`, `README.md`, and the incubation baseline all say
  Apache-2.0 — unresolved contradiction.
- No `__init__.py` below `src/episteme/`; no `.env`, `.editorconfig`, `.gitattributes`,
  `.pre-commit-config.yaml`; bare `print()` throughout.

---

## 3. Target architecture

### 3.1 Storage model — Hybrid

| Layer | Store | Holds | Written by |
|---|---|---|---|
| `01_raw/<source>/` | filesystem, immutable | native downloads (XML.gz, JSON, tarballs) | download stage |
| `02_processed/staging/<source>/*.parquet` | filesystem | normalised `episteme.articles` rows, one Parquet shard per input file | extract stage |
| **Postgres `episteme` schema** | PostgreSQL 19 | structured `articles` (incl. `title`/`abstract`/`body_text` for RAG + BM25 + vector), `id_map`, graph property tables (`article_cites`, `article_mesh`), `chunks` (Phase 1 scaffold), `_runs`, `_lineage`, `_audit` (append-only, hash-chained) | load + graph stages; audit trail |
| `03_corpus/pretrain/*.parquet` | filesystem | materialised flat-`text` training shards, filtered (`subset='commercial' AND extract_status='ok'` …) | materialize stage |
| **OpenMetadata** (Postgres-backed) | service | catalog + lineage over the `episteme` schema and the Parquet corpus | enrich stage → manifest; orchestrator → `metadata ingest` (opt-in) |

**Rationale for hybrid over all-Postgres or all-Iceberg:**
- Training loaders (`datasets`, `polars`, streaming) read Parquet far faster than they stream
  tens of millions of full-text rows out of Postgres; Parquet corpus shards also snapshot
  cleanly per training run.
- Postgres is the right home for structured query, the citation/MeSH graph (SQL/PGQ-ready
  property tables), transactional idempotent loads, and the Stream 2 RAG store (pgvector +
  pg_search BM25). `docs/02-data-sources.md` already anticipated PostgreSQL for Stream 2.
- Smallest deviation from the binding docs: the Parquet corpus survives; only the "processed
  layer = Iceberg" clause is replaced.

**Graph engine:** property tables queried with **SQL/PGQ** (SQL:2023) where the running server
supports it, **recursive CTE fallback** over the same tables until then — no schema change when
PGQ lands. `article_cites(src_pmid, dst_pmid, …)`, `article_mesh(pmid, descriptor_ui,
descriptor_name, major_topic, qualifiers)`. A guarded `CREATE PROPERTY GRAPH` DDL lives in
`db/schema.sql` behind a server-capability check.

**Extensions enabled from the start:** `vector` (pgvector, Stream 2 dense retrieval),
`pg_search` (BM25 lexical retrieval). `db/extensions.sql`, each `CREATE EXTENSION IF NOT EXISTS`
guarded so a missing extension degrades to a warning, not a hard failure, during scaffolding.

### 3.2 `src/` layout and naming convention

```
src/episteme/
  __init__.py
  config.py                          # THE only reader of os.environ; loads .env via python-dotenv; typed settings
  logging_setup.py                   # configure_logging(); modules use logging.getLogger(__name__) — operational logs only
  audit_trail.py                     # NEW — ALCOA+ tamper-evident audit trail; writes episteme._audit (txn-coupled) + JSONL mirror
  data/
    __init__.py                      # documents source-name <-> folder map
    article_schema.py                # was schema.py  — canonical episteme.articles row + license/status/text helpers
    checkpoint_markers.py            # was ops.py     — per-input success/failed/run markers (contract §2)
    staging_writer.py                # was writer.py  — normalised rows -> 02_processed/staging/<source>/*.parquet
    postgres_loader.py               # NEW — staging Parquet -> PG episteme.articles / id_map;
                                     #        txn per input file: DELETE WHERE source_file=$1 then COPY
    graph_builder.py                 # NEW — loaded rows + raw ref lists -> episteme.article_cites, episteme.article_mesh
    corpus_materializer.py           # NEW — PG episteme.articles -> 03_corpus/pretrain/*.parquet (filtered)
    openmetadata_manifest.py         # NEW — build the OpenMetadata ingestion manifest (pure; no server I/O)
    enrich_openmetadata.py           # NEW — CLI: (re)generate + jsonschema-validate the manifest,  --source
    load_articles.py                 # NEW — CLI front over postgres_loader,  --source
    sample_audit.py                  # NEW — field-coverage audit on a sample (contract §8; the missing analyze_source_samples)
    db/
      __init__.py
      schema.sql                     # articles, id_map, article_cites, article_mesh, chunks (Phase 1), _runs, _lineage;
                                     #   guarded CREATE PROPERTY GRAPH DDL
      extensions.sql                 # CREATE EXTENSION IF NOT EXISTS vector; pg_search;  (guarded)
      connection.py                  # psycopg connection/pool built from config.py DSN parts
    pubmed/
      __init__.py  download_pubmed.py  extract_pubmed.py
    pmc/
      __init__.py  download_pmc.py  extract_pmc.py            # download_pmc.py MOVED from scripts/download_pmc_oa_comm.py
    europepmc/
      __init__.py
      preprints/      __init__.py  download_europepmc_preprints.py    extract_europepmc_preprints.py
      manuscripts/    __init__.py  download_europepmc_manuscripts.py  extract_europepmc_manuscripts.py
      id_mappings/    __init__.py  download_europepmc_id_mappings.py                   # -> episteme.id_map
      lite_metadata/  __init__.py  download_europepmc_lite_metadata.py                 # enrichment feed
      abstracts/      __init__.py  download_europepmc_abstracts.py                     # deferred stub
    apollo/
      __init__.py  download_apollo.py  extract_apollo.py
    curate/
      __init__.py
      serialize_structured_sources.py   # was preprocess.py (uniprot/chembl/openmedtext -> prose)
      deduplicate_corpus.py             # was dedup.py (MinHash LSH)
      decontaminate_benchmarks.py       # was decontaminate.py
  model/
    __init__.py
    train_continual_pretraining.py      # was train_cpt.py
    train_supervised_finetuning.py      # was train_sft.py
    train_preference_optimization.py    # was train_preference.py
    evaluate_benchmarks.py              # was evaluate.py
```

**Convention rules:**

1. **Folder = subject area** (the data source): `pubmed`, `pmc`, `europepmc`, `apollo`.
   Europe PMC's five feeds are sub-folders under `europepmc/`.
2. **Filename = `<stage>_<subjectarea>[_<feed>].py`**, fully spelled out — redundant with the
   folder, deliberately, so every basename is unique and self-describing in editor tabs,
   `grep`, and stack traces.
3. **Stage verbs are a closed set:** `download`, `extract`, `load`, `graph`, `enrich`
   (+ `verify` where a source needs checksum repair; `train` / `evaluate` for model).
4. **Shared infra modules are `<noun>_<qualifier>`** and name their technology when bound to
   one: `iceberg`→gone; `postgres_loader`, `openmetadata_manifest`, `enrich_openmetadata`,
   `graph_builder`, `corpus_materializer`.
5. **`load_articles.py` / `enrich_openmetadata.py` are single generic modules** (`--source`
   arg); the unified `episteme.articles` target does not need per-source copies. The
   per-source split lives only in `scripts/`.
6. **No acronyms in filenames:** `europepmc` not `epmc`, `continual_pretraining` not `cpt`,
   `preference_optimization` not `dpo`. **Kept:** `pubmed`, `pmc` — literal NCBI product
   names used in every upstream URL, doc, and the S3 bucket (`pmc-oa-opendata`).
7. **`article_schema.SOURCES` wire values aligned** the same way: `epmc_preprint` →
   `europepmc_preprint`, etc. Only sample data exists on disk, so nothing to migrate.
8. `data_pipeline/` and `model_pipeline/` cease to exist — fully migrated, `git mv` preserving
   history.

### 3.3 Pipeline stages

Ops markers: `02_processed/_ops/<source>/{success,failed,load_success,graph_success,runs,catalog}/`.

| Stage | per-source script | Python entry | reads → writes |
|---|---|---|---|
| download | `download_<source>.sh` | `episteme.data.<source>.download_<source>` | upstream → `01_raw/<source>/` + `remote_manifest.txt`, `last_sync_utc.txt` |
| extract | `extract_<source>.sh` | `episteme.data.<source>.extract_<source>` | `01_raw/<source>/` → `staging/<source>/*.parquet` + `_ops` markers |
| load | `load_<source>.sh` | `episteme.data.load_articles --source <source>` | `staging/<source>/*.parquet` → PG `articles`/`id_map` (txn: delete-by-`source_file` + COPY) + `_ops/<source>/load_success/*` + `episteme._lineage` row |
| graph | `graph_<source>.sh` | `episteme.data.graph_builder --source <source>` | loaded rows + raw ref lists → PG `article_cites`, `article_mesh` |
| enrich | `enrich_<source>.sh` | `episteme.data.enrich_openmetadata --source <source>` | PG catalog + schema → `_ops/<source>/catalog/<source>.openmetadata.json` |
| *(ingest)* | orchestrator only | `metadata ingest -c <manifest>` | manifest → live OpenMetadata — **only if `OM_HOST` set** |
| materialize | `materialize_corpus.sh` | `episteme.data.corpus_materializer` | PG `articles` → `03_corpus/pretrain/*.parquet` |

- **First-time == incremental**: identical commands. Per-input `.ok` markers make re-runs skip
  completed work; `--force` reprocesses; `--since YYYY-MM-DD` on daily feeds (PubMed
  updatefiles, PMC continuous delta, EPMC manuscript incrementals).
- **Feed-specific stage sets**: `id_mappings` runs download → load only (→ `id_map`, after a
  mandatory gzip integrity check — see runbook known-issue). `lite_metadata` runs
  download → enrich only. `abstracts` is a deferred download-only stub.
- **Extract / load are separate steps by design** (operator request): `extract_*` stops at
  staging Parquet with no DB knowledge; `load_articles` is the only thing that touches
  Postgres. A schema or catalog change re-runs `load` over existing staging without re-parsing
  raw.

### 3.4 `scripts/` layout + orchestrator

```
scripts/data/
  run_pipeline.sh                 # run_pipeline.sh <source> [all|download|extract|load|graph|enrich|materialize]
                                  #   sequences stages; stop-on-first-failure; resumable;
                                  #   runs `metadata ingest` after enrich iff OM_HOST set;
                                  #   passes --since / --force / --max-files through to every stage
  _lib/
    common.sh                     # sources .env; venv activate; PYTHONPATH=src; dep/extension checks;
                                  #   log() / die() / require_env(); no hardcoded hosts, paths, URLs
  pubmed/        download_pubmed.sh  extract_pubmed.sh  load_pubmed.sh  graph_pubmed.sh  enrich_pubmed.sh  verify_pubmed.sh
  pmc/           download_pmc.sh  extract_pmc.sh  load_pmc.sh  graph_pmc.sh  enrich_pmc.sh
  europepmc/
    preprints/     download_europepmc_preprints.sh   extract_europepmc_preprints.sh   load_europepmc_preprints.sh   graph_europepmc_preprints.sh   enrich_europepmc_preprints.sh
    manuscripts/   download_europepmc_manuscripts.sh extract_europepmc_manuscripts.sh load_europepmc_manuscripts.sh enrich_europepmc_manuscripts.sh
    id_mappings/   download_europepmc_id_mappings.sh load_europepmc_id_mappings.sh
    lite_metadata/ download_europepmc_lite_metadata.sh enrich_europepmc_lite_metadata.sh
    abstracts/     download_europepmc_abstracts.sh                       # deferred stub
  apollo/        download_apollo.sh  extract_apollo.sh  load_apollo.sh  enrich_apollo.sh
  materialize_corpus.sh
  run_sample_audit.sh
  db/            init_database.sh  migrate_database.sh
```

- **Uniform interface**: every stage script `<script>.sh [RAW_DIR] [PROCESSED_DIR] [MAX_FILES] [-- extra…]`,
  all sourcing `_lib/common.sh`. Defaults come from `.env`
  (`EPISTEME_RAW_ROOT`, `EPISTEME_PROCESSED_ROOT`, …), never literals.
- **Scaffold-in-advance**: every file above is created in the first scaffolding commit.
  Deferred feeds and not-yet-migrated stages land as stubs:
  `.sh` → `echo "not implemented: <purpose>" >&2; exit 2`;
  `.py` → module docstring + `def main(): raise NotImplementedError("<purpose>")`.
- The pre-existing pubmed helpers (`download_pubmed_baseline_md5.sh`,
  `pubmed_baseline_downloader.sh`, `download_pubmed_updates.sh`,
  `verify_pubmed_checksums.sh`, `repair_pubmed_failed_checksum.sh`) consolidate into
  `pubmed/download_pubmed.sh` (baseline + `--since` incremental) and `pubmed/verify_pubmed.sh`
  (verify + repair). `git mv` where a mapping is 1:1; rewrite where merging.

### 3.5 Configuration & code-management practices

- **`.env`** (gitignored) + **`.env.example`** (committed, documents every variable with a safe
  default). Variables include: `EPISTEME_RAW_ROOT`, `EPISTEME_PROCESSED_ROOT`,
  `EPISTEME_CORPUS_ROOT`, `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD`,
  `OM_HOST`/`OM_JWT`, `NCBI_API_KEY`, `EPISTEME_DOWNLOAD_THREADS`, `EPISTEME_SAMPLE_LIMIT`,
  upstream endpoints (`NCBI_FTP_HOST`, `PMC_S3_BUCKET`, `EBI_FTP_HOST`, `EUROPEPMC_BASE_URL`,
  `APOLLO_HF_REPO`).
- **`src/episteme/config.py`** is the single reader of `os.environ` (via `python-dotenv`),
  exposing a typed `Settings` object. No other module reads `os.environ` directly.
- **`_lib/common.sh`** sources `.env` if present, so every shell script sees the same
  variables. Shell scripts reference `$EPISTEME_RAW_ROOT`, `$PGHOST`, `$PMC_S3_BUCKET`, … —
  no hardcoded hosts, bucket names, paths, HF repo ids, thread counts, or sample limits.
- **Invariant constants stay in code** — schema column lists, license-normalisation rules,
  MinHash parameters, SQL keywords. 12-factor targets environment-varying config, not every
  literal. `article_schema.py` keeps `ARTICLE_COLUMNS`, `SOURCES`, `SUBSETS`, etc.
- **`src/episteme/logging_setup.py`**: `configure_logging(level, json=False)`; every module
  uses `logging.getLogger(__name__)` — no bare `print()` in library code (CLIs may still print
  user-facing progress).
- **`pyproject.toml`** (extend the existing file):
  - `[project.optional-dependencies]`: `data` (pyarrow, `psycopg[binary]`, pgvector,
    python-dotenv, requests, tqdm, lxml), `model` (torch, transformers, peft, trl, accelerate,
    datasets, evaluate, scikit-learn), `catalog` (`openmetadata-ingestion`, version-pinned —
    heavy, ingest-only), `dev` (pytest, jsonschema, ruff, testcontainers). Base install minimal.
  - `[tool.ruff]` (lint + format), `[tool.pytest.ini_options]` (`testpaths=["tests"]`, markers
    `pg` and `slow`), `[tool.setuptools.packages.find]` unchanged (`where=["src"]`).
  - `requires-python` stays `>=3.10`.
- **`.pre-commit-config.yaml`**: `ruff` (lint + format), `end-of-file-fixer`,
  `trailing-whitespace`, `check-added-large-files`, `detect-private-key`,
  `check-merge-conflict`. (Would have caught the committed `.pyc`.)
- **`.editorconfig`** (spaces, LF, UTF-8, final newline) and **`.gitattributes`**
  (`*.sh text eol=lf`, `*.ps1 text eol=crlf`, `* text=auto`) — the repo is edited on Windows
  and the shell scripts must stay LF or the shebang breaks.
- **`.gitignore`** gains: `.env`, `graphify-out/`, `02_processed/`, `03_corpus/`, `.entire/`.
- **`LICENSE` reconciliation** — the `LICENSE` file (MIT) contradicts `pyproject.toml` /
  `README.md` / baseline (Apache-2.0). **Operator decision required**: pick one; implementation
  makes all four agree. Default if unspecified: keep Apache-2.0 (permissive + patent grant,
  already in three of four places), replace the `LICENSE` file text.

### 3.6 Tests

```
tests/
  conftest.py                       # shared fixtures: tiny pubmed XML, tiny PMC JSON+XML, tmp dirs
  data/
    test_article_schema.py          # license normalisation, extract_status thresholds, build_text
    test_checkpoint_markers.py      # marker write / skip / replace-on-retry
    test_staging_writer.py          # rows -> parquet round-trip, columnar mapping
    test_curate.py                  # from test_data_pipeline.py (serialize / dedupe / decontaminate)
    test_extract_pubmed.py          # fixture XML -> rows, status rules
    test_extract_pmc.py             # REPLACES broken test_pmc_download.py
    test_postgres_loader.py         # marked `pg`; skips cleanly without TEST_PG_DSN; idempotent replace
    test_graph_builder.py           # marked `pg`; edge population from fixture refs
    test_corpus_materializer.py     # filter correctness, parquet round-trip
    test_openmetadata_manifest.py   # manifest is jsonschema-valid
    test_audit_trail.py             # hash-chain integrity, append-only, ALCOA+ fields present, tamper detection
  model/
    test_model_smoke.py             # from test_model_pipeline.py; imports episteme.model.*
```

- `tests/test_pmc_download.py` **deleted** (tests a signature the upstream pull removed; current
  cause of the red collection).
- `tests/test_data_pipeline.py` → `tests/data/test_curate.py`; imports updated to
  `episteme.data.curate.*`.
- `tests/test_model_pipeline.py` → `tests/model/test_model_smoke.py`; imports
  `episteme.model.*`.
- Postgres-touching tests carry `@pytest.mark.pg` and skip when `TEST_PG_DSN` is unset, so the
  default `pytest -q` is green without a database.
- **Gate:** `pytest -q` green again (currently red at collection).

### 3.7 Documentation updates (same pass)

- **`docs/08-data-storage-principles.md`** — rewritten: processed store = **PostgreSQL**
  (`episteme` schema) for structured articles + graph + `id_map` + RAG; **Parquet** for the
  `03_corpus/` training corpus (materialised from Postgres); **OpenMetadata** (Postgres-backed)
  as catalog/lineage. SQL/PGQ + pgvector + pg_search noted. Keep the layered `01_raw → 02_processed
  → 03_corpus` model and the "raw is sacred" rule.
- **`docs/07-knowledge-graph-lessons.md`** — §3.2 representation choice updated to Postgres
  property tables + SQL/PGQ (recursive-CTE fallback); drop the "defer the graph DB" stance,
  keep the "structured published metadata over LLM-extracted triples" thesis and the
  citation/MeSH edge priorities.
- **`docs/09-extraction-contract.md`** — §4 (Iceberg → Postgres tables + idempotent
  delete-by-`source_file`), §10 entrypoints → final paths, resolve open question #3
  (`source_file` = basename). Add a cross-reference to `docs/11` for the audit-trail obligation
  on every load/replace.
- **`docs/11-gxp-data-integrity.md`** — NEW. Records the ALCOA+ posture (see §3.8), the
  audit-record schema, the hash-chain scheme, retention expectations, and — explicitly — the
  boundary between what this codebase automates (tamper-evident audit trail, provenance,
  no-silent-drops) and what remains procedural for a real GxP qualification (CSV/validation,
  RBAC, e-signatures, periodic review, SOPs). No compliance is *claimed*; the design is stated
  as "GxP-ready," not "GxP-compliant."
- **`docs/10-data-sources-runbook.md`** — rewritten as **the single operator runbook**: DB
  setup (`scripts/data/db/init_database.sh`, `PG*` / `OM_HOST` env), then per source a
  first-time block and an incremental block using the real `scripts/data/<source>/*.sh` paths
  and `run_pipeline.sh`, then the materialize + ingest steps. Keep the existing known-failure
  tables and the ops-cadence table.
- **`docs/project-incubation-baseline.md`** — Drift Log entry: storage decision reversed
  (Iceberg/OpenMetadata-filesystem → Postgres hybrid + Parquet corpus); `src/` + `scripts/`
  restructured to subject-area taxonomy; config extracted to `.env`.
- **`README.md`** — fix the broken `python -m …train_cpt.py` invocations; point the data
  section at `docs/10`.

### 3.8 GxP data-integrity logging & audit trail

The data is medical/pharma-adjacent (open literature, chemical/genomic databases). This phase
builds a **GxP-ready** audit trail — ALCOA+-aligned, tamper-evident — so provenance is
defensible if the model or its RAG layer is later used in a regulated context. **No regulatory
compliance is claimed:** 21 CFR Part 11 / EU Annex 11 also require system validation, access
control, and SOPs that are procedural, not code (see `docs/11`).

**Two tiers, no new runtime dependency** (stdlib `hashlib` + canonical `json`; `jsonschema`
already added for manifest validation):

| Tier | Component | Mutability | Role |
|---|---|---|---|
| Operational logs | `logging_setup.py` → rotating JSON files / stderr | mutable | debugging, progress, ops signal — **not** the record |
| Audit trail | `audit_trail.py` → Postgres `episteme._audit` (in the same transaction as the data change) **+** append-only JSONL mirror `02_processed/_ops/_audit/audit-YYYYMMDD.jsonl` | append-only | the GxP electronic record of every create / modify / delete of data |

**Audit record schema (`episteme._audit`):**

| Field | ALCOA+ | Notes |
|---|---|---|
| `seq` | Consistent | monotonic per chain (`BIGSERIAL`) |
| `recorded_at` | Contemporaneous | UTC `timestamptz`, written in the data change's own transaction |
| `actor` | Attributable | `EPISTEME_ACTOR` env → OS login → `unknown` |
| `host`, `pid`, `run_id` | Attributable | `run_id` correlates with `episteme._runs` |
| `code_version` | Original/Accurate | git SHA of the running tree |
| `event_type` | — | enum: `run_start`, `run_end`, `extract_commit`, `load_commit`, `load_replace`, `graph_commit`, `corpus_materialize`, `schema_migration`, `force_override`, `integrity_check`, `manual_correction`, `config_change` |
| `object` | — | e.g. `episteme.articles source_file=pubmed26n0001.xml.gz` |
| `input_content_hash` | Original | SHA-256 of the raw input file, when applicable |
| `rows_affected` | Accurate | inserted / deleted counts (`load_replace` records the delete count) |
| `old_value`, `new_value` | Original/Accurate | JSON, for modifications and corrections |
| `reason` | Attributable | **required** for `force_override`, `manual_correction`, `schema_migration` (operator passes `--reason "…"`); optional for routine automated events |
| `prev_hash`, `record_hash` | — (integrity) | `record_hash` = SHA-256 over canonical-JSON of all other fields incl. `prev_hash`; chain is tamper-evident |

**Integrity & retention:**
- The application DB role is granted `INSERT` + `SELECT` on `episteme._audit` — **never
  `UPDATE` / `DELETE`** (enforced by `GRANT`s in `db/schema.sql`).
- JSONL mirror is documented append-only (`chattr +a` on Linux where available; the mirror
  exists so the trail survives a DB restore/rebuild).
- `scripts/data/verify_audit_trail.sh` → `episteme.data`-level verifier walks the chain
  (`prev_hash` links, `record_hash` recomputation) across both the table and the mirror and
  reports the first divergence; a break is a hard failure, not a warning.
- Retention: the pipeline never deletes audit records. Retention *period* is an
  operator/QA decision recorded in `docs/11` (default posture: retain for the life of any
  corpus or model derived from the data).
- No silent drops — ties to extraction-contract §1; a `dropped` row still produces an audit
  event.

**Wiring:** `postgres_loader.py`, `graph_builder.py`, `corpus_materializer.py`, and the
`db/migrate_database.sh` runner each emit their audit events through `audit_trail.py` inside
the transaction they commit. `run_pipeline.sh` emits `run_start` / `run_end`. `--force` on any
stage emits `force_override` and refuses to proceed without `--reason`.

---

## 4. Migration order

Branch `refactor/data-taxonomy-and-postgres` (created). Each numbered step is its own commit;
`pytest -q` is green or explicitly `pg`-skipped between every step.

1. **Scaffold** the full `data/` + `model/` + `db/` tree — every `__init__.py`, every stub
   `.py` / `.sh`, `config.py`, `logging_setup.py`, `audit_trail.py` (stub), `.env.example`,
   `.editorconfig`, `.gitattributes`, `.pre-commit-config.yaml`, `.gitignore` additions.
   Nothing moved yet; old tree intact; `pytest` still green.
2. `git mv` `model_pipeline/` → `model/`; rename the four modules; fix imports; move
   `test_model_pipeline.py` → `tests/model/test_model_smoke.py`. Model tests green.
3. `git mv` `data_pipeline/{dedup,decontaminate,preprocess}.py` →
   `data/curate/{deduplicate_corpus,decontaminate_benchmarks,serialize_structured_sources}.py`;
   fix imports; split `test_data_pipeline.py` → `tests/data/test_curate.py`. Green.
4. `git mv` `scripts/download_pmc_oa_comm.py` → `data/pmc/download_pmc.py`; delete the
   `sys.path` hack in the old `data_pipeline/download.py`; delete `tests/test_pmc_download.py`;
   add `tests/data/test_extract_pmc.py`. `pytest -q` green (fixes the current red).
5. Migrate `data_pipeline/download.py` logic into per-source `download_*.py`; remove
   `data_pipeline/` entirely.
6. `git mv` `data/epmc/preprint_extract.py` →
   `data/europepmc/preprints/extract_europepmc_preprints.py`; rename `data/epmc/` →
   `data/europepmc/`; build the five feed sub-folders.
7. Fill `article_schema.py` (rename from `schema.py`, align `SOURCES`),
   `checkpoint_markers.py` (rename from `ops.py`), `staging_writer.py` (rename from
   `writer.py`), `audit_trail.py`, `db/{schema.sql,extensions.sql,connection.py}` (incl.
   `episteme._audit` + its `INSERT`/`SELECT`-only `GRANT`s), `postgres_loader.py`,
   `load_articles.py`, `graph_builder.py`, `corpus_materializer.py`,
   `openmetadata_manifest.py`, `enrich_openmetadata.py`, `sample_audit.py`, and their tests
   (incl. `test_audit_trail.py`). `postgres_loader` / `graph_builder` / `corpus_materializer`
   emit audit events inside their commit transactions.
8. `scripts/data/` tree: `_lib/common.sh`, per-source stage scripts (consolidate the ~15 flat
   scripts — `git mv` where 1:1, rewrite where merging), `run_pipeline.sh` (emits
   `run_start` / `run_end`, enforces `--reason` with `--force`), `db/init_database.sh`,
   `db/migrate_database.sh`, `verify_audit_trail.sh`, `materialize_corpus.sh`,
   `run_sample_audit.sh`.
9. `pyproject.toml` dependency groups + tooling config; `LICENSE` reconciliation; `README.md`.
10. Rewrite docs 07 / 08 / 09 / 10; baseline Drift Log entry.
11. **Green gates** (§5).

---

## 5. Verification / acceptance

- `pytest -q` green (no `pg` DB needed; `pg`-marked tests skip cleanly).
- Fresh venv: `pip install -e ".[data]"` clean; then
  `python -c "import episteme.config, episteme.data.pubmed.extract_pubmed, episteme.data.load_articles, episteme.model.train_continual_pretraining"`.
- `scripts/data/db/init_database.sh` against a local Postgres → `episteme` schema present;
  `vector` + `pg_search` extensions present (or a clear skipped-with-warning line if
  unavailable).
- `scripts/data/run_pipeline.sh pmc all --max-files 2` on the existing `01_raw/pmc/oa_comm/`
  sample →
  - `02_processed/staging/pmc/*.parquet` written,
  - `episteme.articles` has 2 rows with correct `source` / `subset` / `license` /
    `extract_status`,
  - `episteme._lineage` has a row for each input file,
  - `episteme._audit` has `run_start` + `load_commit` (×2) + `run_end` events, each with a
    valid `prev_hash` / `record_hash` chain and a matching JSONL-mirror line,
  - `scripts/data/verify_audit_trail.sh` reports the chain intact,
  - the app DB role cannot `UPDATE` or `DELETE` `episteme._audit` (negative check),
  - `_ops/pmc/catalog/pmc.openmetadata.json` validates against the manifest schema,
  - `03_corpus/pretrain/*.parquet` shard materialised.
- No module other than `config.py` references `os.environ`
  (`grep -rn "os.environ\|getenv" src/` → only `config.py`).
- No hardcoded upstream host / bucket / path literal in `scripts/`
  (spot-check `grep -rn "ftp\.\|s3\.\|https://" scripts/data/` → only `.env`-sourced vars).
- `git log --follow` shows history preserved across the renames.
- graphify `/graphify . --update` re-runs clean against the new layout (optional).

---

## 6. Open items requiring an operator decision

1. **`LICENSE`**: MIT (current file) or Apache-2.0 (everything else)? Default: Apache-2.0.
2. **SQL/PGQ availability** in the PG 19 build in use — confirmed present, or design proceeds
   with recursive-CTE queries and the PGQ DDL guarded/dormant? (No schema impact either way.)
3. **`openmetadata-ingestion` version pin** — latest compatible with the OM server version you
   intend to run.
4. **Audit-trail retention period** and whether electronic signatures are in scope this phase
   (assumed **no** for Phase 0) — QA/operator input, recorded in `docs/11`.
5. **`EPISTEME_ACTOR` identity source** — CI/service-account name vs. individual operator login
   for attributable audit records.
