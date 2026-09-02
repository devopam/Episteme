# Phase-0 Data Roadmap — architecture spec and sub-project charter

**Date:** 2026-09-02
**Status:** Approved (brainstorm) — sub-plans execute against this
**Type:** Roadmap / umbrella spec. Subsumes the "Plan 2 / Plan 3" outline in
`docs/superpowers/specs/2026-09-01-data-taxonomy-and-postgres-restructure-design.md`
(whose Phase-1 restructure is now merged to `main`).
**Purpose:** One coherent design for *all* remaining Phase-0 data work, so the storage
layer, the literature extractors, the ~13 newly-arrived structured sources, the
`scripts/` orchestration, and the doc rewrites do not become uncoordinated threads
with integration gaps.

---

## 1. Scope boundary — Stream 1 only

`docs/01-strategy-summary.md` and `docs/02-data-sources.md` define a bimodal strategy:

- **Stream 1 (Phase 0, this roadmap):** open, commercially-usable, *relatively stable*
  knowledge baked into the model's parametric weights. Output: `episteme.articles`
  (Postgres) → `03_corpus/pretrain/*.parquet` training shards.
- **Stream 2 (Phase 1, out of scope here):** proprietary, customer-specific, versioned,
  or *volatile* data served at run time via RAG + a knowledge/graph layer. **Its shape
  is still changing** — this roadmap deliberately does not design it.

**Phase-0 deliverable per source class:**

| Class | Sources | Phase-0 path |
|---|---|---|
| **Literature** | `pubmed`, `pmc`, `bookshelf`, `europepmc_preprint`, `europepmc_manuscript`, `apollo`, `guidelines` | `extract_` → `episteme.articles` → **graph** (`article_cites`, `article_mesh`) + `03_corpus` |
| **EPMC support feeds** | `europepmc/id_mappings` (→ `episteme.id_map`), `europepmc/lite_metadata` (→ enrichment) | download + load/enrich, no corpus rows |
| **Stable structured** | `chembl`, `uniprot`, `pubchem`, `clinvar`, `reactome`, `mesh`, `ontologies`, `openalex` | `serialize_` → declarative-prose rows → `episteme.articles` → `03_corpus` (no graph, except `mesh` descriptors feed `article_mesh`) |
| **Volatile (Stream 2 / Phase 1)** | `dailymed`, `openfda`, `aact` | `download_` → `01_raw/<source>/` + provenance **only**. No `serialize_`, no `articles` rows this phase. |
| **Acquisition mechanism** | `hf_corpus` (generic `<repo_id>` wrapper) | shared helper. SFT/eval HF sets (MedMCQA, PubMedQA) route to `04_evals/`, not the corpus. |

The citation/MeSH **property tables** are *populated* in Phase 0 from literature metadata
(zero-LLM, per `docs/07`). The graph *traversal / retrieval* layer is Phase 1.

---

## 2. Unified pipeline model

Every source — literature or structured — produces `article_schema`-shaped rows.
`serialize_` (structured input: SQLite dump, TSV, RDF, bulk XML → prose) and `extract_`
(literature input: streaming JATS/MEDLINE XML → title/abstract/body) are **siblings with
the same output contract** and nothing else shared (no base class — the input parsers
have no common ground).

```
download_<source>.sh        →  01_raw/<source>/  (+ remote_manifest.txt, last_sync_utc.txt) — immutable
  ↓
extract_<source>.py                                     (literature)
  OR serialize_<source>.py                              (structured)
                            →  02_processed/staging/<source>/<input_basename>.parquet
  ↓
load_articles --source <s>  →  episteme.articles  (+ episteme.id_map for id_mappings)
                               + episteme._audit  (txn-coupled, hash-chained)
                               + episteme._lineage
  ↓
graph_builder --source <s>  →  episteme.article_cites, episteme.article_mesh   (literature only; mesh descriptors from `mesh`)
  ↓
corpus_materializer         →  03_corpus/pretrain/*.parquet
                               filter: subset='commercial' AND extract_status='ok', post dedup + decontamination
  ↓
enrich_openmetadata --source →  _ops/<source>/catalog/<source>.openmetadata.json
                               + orchestrator runs `metadata ingest` iff OM_HOST set
```

Storage (from the 2026-09-01 spec, unchanged): **Postgres 19** hybrid — `episteme.articles`
(structured + full text for later RAG) + graph property tables + `id_map` + `_runs` +
`_lineage` + `_audit`; **Parquet** for `03_corpus`; **OpenMetadata** (Postgres-backed)
catalog. SQL/PGQ committed for the graph; `pgvector` + `pg_search` extensions enabled but
unused until Phase 1.

---

## 3. Sub-project charter

Five sub-plans. **SP1 is the foundation; SP2 ∥ SP3 ∥ SP4 fan out from it; SP5 trails.**
Each sub-plan gets its own short **spec-delta** → `superpowers:writing-plans` →
`superpowers:subagent-driven-development` → merge to `main`.

### SP1 — Storage core + proving slice

**Scope.** Renames + fills: `schema.py`→`article_schema.py`, `ops.py`→`checkpoint_markers.py`,
`writer.py`→`staging_writer.py`. New: `db/{schema.sql,extensions.sql,connection.py}` (partitioning
per ADR-0002); real `audit_trail.py`; `postgres_loader.py` + `load_articles.py`; `graph_builder.py`;
`corpus_materializer.py` — which wires the existing `data/curate/deduplicate_corpus` (MinHash LSH,
`content_hash` near-dup) and `data/curate/decontaminate_benchmarks` (13-gram scrub vs the eval sets)
into the materialisation step so `03_corpus` is de-duplicated and decontaminated by construction;
`openmetadata_manifest.py` + `enrich_openmetadata.py`; `sample_audit.py`.
**ADR-0001** (hybrid storage) + **ADR-0002** (data model, storage, partitioning) authored here,
before `db/schema.sql`. Merge upstream `data/pmc/extract.py` logic → `extract_pmc.py`; retire the
old-named file. Seed `scripts/data/pmc/{download,extract,load,graph,enrich}_pmc.sh`,
`scripts/data/_lib/common.sh`, `scripts/data/db/init_database.sh`, seed `run_pipeline.sh` (pmc only).

**Depends on.** merged `main` (Phase-1 restructure).

**Exit criteria.**
- `scripts/data/run_pipeline.sh pmc all --max-files 2` on the on-disk `01_raw/pmc/oa_comm/`
  sample: staging Parquet → `episteme.articles` (2 rows, correct `source`/`subset`/`license`/
  `extract_status`) → `episteme._lineage` row per input → `episteme._audit` `run_start` +
  `load_commit`×2 + `run_end` with a valid `prev_hash`/`record_hash` chain + JSONL mirror →
  `article_mesh` populated → `03_corpus/pretrain/*.parquet` shard → OM manifest validates.
- `verify_audit_trail.sh` reports the chain intact; app DB role cannot `UPDATE`/`DELETE` `_audit`.
- **PMC field-shape report produced** (§4.2) and any `article_schema` v1.1→v1.x deltas applied.
- `pytest -q` green; fresh-venv `pip install -e ".[data]"` + import check.

### SP2 — Remaining literature

**Scope.** Fill/add `extract_pubmed.py`, `extract_europepmc_preprints.py` (fill),
`extract_europepmc_manuscripts.py`, `extract_apollo.py`, `extract_bookshelf.py`,
`extract_guidelines.py` (Meditron via `hf_corpus`). `europepmc/id_mappings` → `episteme.id_map`
(after the mandatory gzip integrity check, `docs/10` §5). `europepmc/lite_metadata` → enrichment.
Per-source field-shape report + schema sign-off before full load.

**Depends on.** SP1 contract (§4).

**Exit criteria.** Each source: fixture → `article_schema` rows with correct `extract_status`
rules; one real end-to-end (`run_pipeline.sh <source> all --max-files …`) producing `articles`
rows + a `03_corpus` shard; field-shape report signed.

### SP3 — Acquisition layer

**Scope.** Build `scripts/data/<source>/` nested tree (§4.1) for **all ~17 wired sources**;
`_lib/common.sh` (§4.6) + `_lib/hf_download.sh`; full `run_pipeline.sh`. Normalise the 16
batch scripts (in scratch) + `download_bookshelf_oa.sh` (on `main`, flat) + existing
`scripts/*.sh` into the nested layout: each re-expressed as a thin wrapper over `_lib`
helpers, `.env`-driven, no hardcoded hosts/paths. **Adopt the 4 EPMC rewrites**
(`download_europepmc.sh`, `download_author_manuscripts.sh`, `download_epmc_id_mappings.sh`,
`download_epmc_lite_metadata.sh` — dated 2026-08-31, substantial) as the normalisation base;
retire the repo copies. `.env.example` grows every new var (§4.5). Volatile sources
(`dailymed`, `openfda`, `aact`) get `download_<source>.sh` only.

**Depends on.** SP1's `.env` + `_lib` contract — **frozen in §4 of this roadmap**, so SP3
can start as soon as SP1 lands `_lib/common.sh`'s skeleton (or in parallel against §4).

**Exit criteria.** `run_pipeline.sh <source> download` works for every wired source (dry-run
or `--max-files 1` where feasible); `shellcheck` clean; no `grep -E 'ftp\.|s3\.|https://'`
literal in `scripts/data/**` except via `.env`/`_lib` defaults.

### SP4 — Structured serializers

**Scope.** `serialize_chembl.py`, `serialize_uniprot.py`, `serialize_pubchem.py`,
`serialize_clinvar.py`, `serialize_reactome.py`, `serialize_mesh.py` (+ its descriptor tree →
`article_mesh` via `graph_builder`), `serialize_ontologies.py`, `serialize_openalex.py`
(scope decided in-plan — see §6). Port the old `preprocess.py` ChEMBL/UniProt serializers
(now in `data/curate/serialize_structured_sources.py`) into the per-source modules. Per-source
field-shape report + schema sign-off before full load. Each emits `id = "<source>:<native_id>"`,
sparse bib fields, `text` = declarative prose, `subset` from `normalize_license`.

**Depends on.** SP1 contract; SP3 download scripts (or sample raw fixtures).

**Exit criteria.** Each: dump/bulk fixture → prose rows → `articles` → `03_corpus` shard;
field-shape report signed; a spot-check that the prose is factually faithful to the source record.

### SP5 — Docs & governance

**Scope.** Rewrite `docs/07` (graph → Postgres property tables + SQL/PGQ), `docs/08`
(Postgres hybrid storage), `docs/09` (extraction contract → Postgres + a structured-source
serialisation addendum + the finalised `article_schema` table), `docs/10` (the operator
runbook — now ~17 sources, first-time + incremental blocks, `run_pipeline.sh`, DB setup).
New `docs/11-gxp-data-integrity.md` (ALCOA+ posture, automated-vs-procedural boundary; from
the 2026-09-01 spec §3.8). Reconcile with the shadow-work `docs/02` rewrite already on `main`.
Living source inventory (source, class, licence, cadence, last sync, row count). Baseline
drift-log entry.

**Depends on.** Final shape of SP1–SP4. ADR-0001/0002 already landed in SP1.

**Exit criteria.** Docs match the shipped tree; runbook covers every wired source; a
non-developer can run the full chain for one source from `docs/10` alone.

### Build order

```
SP1  ──►  SP2 ┐
          SP3 ├─ parallel  ──►  SP5 (finalised last)
          SP4 ┘
```
If a sub-plan surfaces something that must change a §4-frozen interface, it stops, the change
is ruled on against this roadmap, the roadmap gets a dated drift-log entry, and dependent
sub-plans are notified. That is the gap-catch mechanism.

---

## 4. Frozen conventions

Eight interfaces are **frozen now** — sub-plans bind to them verbatim. The `article_schema`
**row shape** is the ninth interface and is **provisional** (§4.2).

### 4.1 `scripts/data/` layout

Nested by source, verb-named files; product-name acronyms kept (`pmc`, `pubmed`, `aact`,
`openfda`), everything else spelled out.

```
scripts/data/
  run_pipeline.sh          # run_pipeline.sh <source> <all|download|extract|serialize|load|graph|materialize|enrich>
  _lib/common.sh  _lib/hf_download.sh
  pubmed/  pmc/  bookshelf/  apollo/  guidelines/           # literature: download_ extract_ load_ graph_ enrich_ (+ verify_ for pubmed)
  europepmc/preprints|manuscripts|id_mappings|lite_metadata|abstracts/
  chembl/  uniprot/  pubchem/  clinvar/  reactome/  mesh/  ontologies/  openalex/   # structured: download_ serialize_ load_ enrich_ (mesh also graph_)
  dailymed/  openfda/  aact/                                # volatile: download_ only
  materialize_corpus.sh  run_sample_audit.sh  verify_audit_trail.sh  rotate_audit_logs.sh
  db/  init_database.sh  migrate_database.sh
```
Each stage `.sh` = `source _lib/common.sh` → `require_env …` → `exec python -m
episteme.data.<source>.<stage>_<source> "$@"` (or `aria2_fetch` / `aws_sync` for `download_`).

### 4.2 `article_schema` row contract — PROVISIONAL

The columns in the current `schema.py` are the **v1.1 starting point**, not a freeze:
`id, source, source_file, source_record_id, pmid, pmcid, doi, title, abstract, body_text,
text, authors, journal, year, mesh, publication_types, language, license, license_url,
license_raw, subset, is_retracted, extract_status, extract_notes, retrieved_at, content_hash,
pmc_version, is_manuscript, is_historical_ocr, pdf_url`.

**Refinement protocol (binding on every source, SP1's PMC slice first):**
1. Acquire a small **real** sample.
2. `sample_audit` → a one-page **field-shape report**: columns this source populates, null
   rates, licence strings actually seen, `extract_status` distribution, anything surprising.
3. Propose schema deltas (additive columns, type changes, `extract_status` threshold tweaks,
   `normalize_license` additions).
4. **Operator sign-off** on the report + deltas.
5. *Then* wire the full load.
No source loads at scale before its report is signed.

`article_schema.SCHEMA_VERSION` + an in-file changelog track deltas; `docs/09` §3's table is
updated each refinement. Re-loads use idempotent `delete-by-source_file` + append, so a
mid-programme refinement re-materialises cleanly.

`SOURCES` (frozen as a *list*, the values are stable identifiers): `pubmed pmc bookshelf
europepmc_preprint europepmc_manuscript europepmc_lite apollo guidelines chembl uniprot
pubchem clinvar reactome mesh ontologies openalex`. `dailymed openfda aact` reserved,
not emitted in Phase 0.

### 4.3 `staging_writer` API (frozen)

```python
write_rows(rows: list[dict], staging_root: Path, *, source: str, source_file: str,
           prefer_parquet: bool = True) -> dict   # {"format","paths","n_rows"}
```
Writes one shard per input file under `02_processed/staging/<source>/`. JSONL fallback when
pyarrow is unavailable.

### 4.4 `checkpoint_markers` API (frozen)

`ops_root/success_marker_path/failed_marker_path/is_success/mark_success/mark_failed/
write_run_manifest/list_input_files` (current `ops.py` signatures). Ops layout:
`02_processed/_ops/<source>/{success,failed,load_success,graph_success,runs,catalog}/`
plus a cross-source `02_processed/_ops/_audit/`.

### 4.5 `.env` variables (frozen names; SP3 writes `.env.example`)

`EPISTEME_RAW_ROOT EPISTEME_PROCESSED_ROOT EPISTEME_CORPUS_ROOT EPISTEME_ACTOR
EPISTEME_DOWNLOAD_THREADS EPISTEME_SAMPLE_LIMIT` · `PGHOST PGPORT PGDATABASE PGUSER
PGPASSWORD` · `OM_HOST OM_JWT` · `NCBI_API_KEY` · one `<SOURCE>_BASE` per source
(`CHEMBL_BASE UNIPROT_BASE PUBMED_FTP_BASE PMC_S3_BUCKET EUROPEPMC_BASE OPENALEX_S3
DAILYMED_BASE OPENFDA_CATALOG AACT_DOWNLOADS CLINVAR_BASE PUBCHEM_BASE REACTOME_BASE
MESH_BASE BOOKSHELF_BASE …`) — defaults in `_lib` / `config.py`, overridable.
`config.py` remains the **only** `os.environ` reader on the Python side.

### 4.6 `_lib/common.sh` function set (frozen)

```
log LEVEL MSG            die MSG [CODE]            require_env VAR [VAR...]
load_dotenv              discover_manifest URL DEST
size_match_skip FILE URL aria2_fetch URL_LIST DEST aws_sync S3_URI DEST [ARGS...]
write_sync_stamp DEST
```
`require_env EPISTEME_ACTOR` runs before any stage that writes an audit record.

### 4.7 Python CLI contract (frozen)

```
python -m episteme.data.<source>.<stage>_<source> \
  --raw-dir DIR --processed-dir DIR --max-files N [--force] [--since YYYY-MM-DD] [--workers N]
```
Config for anything not passed comes from `episteme.config.get_settings()`. Each stage emits
`audit_trail.record(event_type, **fields)` inside the transaction it commits. `--force`
requires `--reason`.

### 4.8 `audit_trail.record` (frozen)

`record(event_type: str, **fields) -> None` — writes `episteme._audit` (in the caller's
transaction) + the append-only JSONL mirror, hash-chained. Event types: `run_start run_end
extract_commit serialize_commit load_commit load_replace graph_commit corpus_materialize
schema_migration force_override integrity_check manual_correction config_change`. ALCOA+
fields per the 2026-09-01 spec §3.8.

### 4.9 Idempotency & restart (frozen)

Unit of work = one input file. Success = fully parsed **and** rows committed **and** success
marker written. Retry replaces prior rows for that `source_file` (`DELETE WHERE source_file=$1`
+ append). Per-input `.ok` markers skip completed work; `--force` reprocesses (with `--reason`).
`--since` on daily feeds.

---

## 5. Non-goals

- Stream 2 / RAG / knowledge-layer design (graph traversal, retrieval stack, `pgvector` /
  `pg_search` usage, multi-tenant RBAC) — Phase 1.
- Any `serialize_` / `articles` rows for `dailymed`, `openfda`, `aact`.
- Cross-source deduplication into a single "best" record (only `content_hash` near-dup within
  the corpus job).
- Model training changes.
- A production OpenMetadata deployment (enrich emits a manifest; ingest is opt-in on `OM_HOST`).

---

## 6. Open items (resolved in the owning sub-plan, not here)

1. **`openalex` scope (SP4).** Full snapshot is 100s of GB and ~90% metadata-overlaps
   `pubmed`/`pmc`. Options for its sub-plan: (a) bounded subset (biomedical-filtered OA works
   not already in pmc), (b) download-and-park like the volatile group, (c) citation-source
   only (feeds graph, not corpus). Not "serialize all of it".
2. **`uniprot` licence (SP4).** CC BY-ND — serialising sequences to prose is arguably a
   "derivative". The current `normalize_license` maps ND → `commercial`. SP4 records an
   explicit call (keep, or route UniProt prose to a `text_mining` subset excluded from the
   commercial corpus).
3. **PMC vs PubMed as SP1's proving slice.** PMC chosen — 2 real articles already on disk at
   `01_raw/pmc/oa_comm/`. Confirm no blocker before SP1 kickoff.
4. **`guidelines` source identity.** Meditron guidelines arrive via `hf_corpus`; SP2 decides
   whether it's its own `source` value or folded into a broader `guidelines` bucket that later
   also holds WHO/NICE where licence permits.

---

## 7. Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-09-02 | Initial roadmap from the Phase-0 data brainstorm. |
