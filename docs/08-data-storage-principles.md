# Data Storage Principles – Episteme

**Last updated:** September 2026 (SP5)
**Status:** Binding. Rewritten to the shipped design; supersedes the Iceberg / OpenMetadata-as-store version.
**Authoritative sources:** `docs/adr/0001-hybrid-storage-architecture.md` (ADR-0001), `docs/adr/0002-data-model-storage-and-partitioning.md` (ADR-0002), `src/episteme/data/db/schema.sql`, `src/episteme/config.py`. Where this document and the code disagree, the code wins and the gap is named in section 9.

## 1. Decision

Episteme standardises on a **Postgres-centric hybrid** (ADR-0001):

| Layer | Technology | Role |
|-------|------------|------|
| Raw | Filesystem (`01_raw/`) | Native downloads, immutable |
| Processed, of record | **PostgreSQL** (`episteme` schema) | Structured `articles`, `article_body`, the graph property tables, `id_map`, `_runs`, `_lineage`, `_audit` |
| Processed, hand-off | **Parquet** shards (`02_processed/staging/`) | Transient shard per input file, consumed by `load_articles` |
| Corpus | **Parquet** (`03_corpus/pretrain/`) | Materialised training text, Hive-partitioned by `source` and `year` |
| Catalog | OpenMetadata manifest (`02_processed/_ops/<source>/catalog/`) | A validated JSON file only; see section 5 |

Apache Iceberg is **not** used. No code in `src/` writes an Iceberg table or a `warehouse/` directory. Raw downloads stay in their native form (XML.gz, JSON, tarballs).

## 2. Goals

- Avoid proprietary lock-in (Postgres and Parquet are open; the corpus is plain Parquet readable by `polars`, `duckdb`, `datasets`).
- Support reproducibility and auditability: transactional loads coupled to a tamper-evident audit trail (`docs/11-gxp-data-integrity.md`).
- Keep license, provenance and lineage first-class and queryable (`articles.license`, `license_url`, `subset`, `source_file`, `content_hash`, and the `_lineage` table).
- Scale from a laptop to a larger host without redesign: roots are configuration, not code.

## 3. Layered Storage Model

```text
01_raw/                    -> original downloads, immutable
02_processed/staging/      -> normalised Parquet shards, one per input (transient hand-off)
02_processed/_ops/         -> markers, run manifests, catalog manifest, audit JSONL mirror
Postgres `episteme` schema -> processed data of record + graph property tables + audit trail
03_corpus/pretrain/        -> materialised training corpus (Parquet)
```

### Rules

1. **Raw is sacred.** Never overwrite or fix in place under `01_raw/`.
2. **Processed is derived.** Postgres content is reproducible from raw plus code version. Loads are idempotent per `source_file` (delete then insert in one transaction, see `docs/09-extraction-contract.md` section 4.2).
3. **Corpus is curated.** `03_corpus/` is a per-run snapshot: `subset = 'commercial' AND extract_status = 'ok'`, near-duplicate removal and benchmark decontamination, each run audited (`corpus_materialize`).
4. **Metadata is mandatory.** Every row carries source, license, subset and provenance columns; every load writes a `_lineage` row and an audit event.

## 4. Apache Iceberg — superseded

Superseded by ADR-0001. The original design used Iceberg tables over Parquet as the processed layer; ADR-0001 replaced it because Iceberg provides no graph query engine (a second store would still have been needed), and Postgres already provides transactional loading, partitioning and the audit trail. The former table-naming, catalog and snapshot conventions are not in force and have been removed from this document. If a lakehouse tier is ever added it needs a new ADR.

Non-negotiables from the old section that still hold, restated for Postgres and Parquet:

- No undocumented schema change to a published table: `schema.sql` plus a numbered migration under `src/episteme/data/db/migrations/` is the change record, and ADR-0002's standing rule requires a partitioning addendum for every new table.
- Prefer append and controlled batch loads; avoid many tiny Parquet files (corpus shards are written per `(source, year)`).

## 5. OpenMetadata — superseded as a store

Superseded by ADR-0001 as the metadata **store**. Postgres is the store of record for both data and lineage-relevant metadata. What OpenMetadata is today (`src/episteme/data/enrich_openmetadata.py`, `openmetadata_manifest.py`):

- `enrich_openmetadata` builds a catalog manifest (the `episteme` Postgres service, the requested tables with `episteme.license` / `episteme.subset` column tags, and a `01_raw/<source> -> extract -> load -> articles` lineage edge), validates it against `manifest.schema.json`, and writes `<processed_root>/_ops/<source>/catalog/<source>.openmetadata.json`.
- It then records a best-effort `config_change` audit row; failure of that audit never fails the file write.
- It does **not** contact an OpenMetadata server. `OM_HOST` and `OM_JWT` are read into `Settings` (`om_host`, `om_jwt`) but nothing under `src/` consumes them, so no ingest to a running service exists yet. ADR-0001's "opt-in via `OM_HOST`" is therefore aspirational.

Non-negotiables that still hold:

- No silent mixing of non-commercial data into commercial training data (enforced by the `subset` filter at materialisation).
- License and provenance must remain queryable (they are columns in `episteme.articles`).
- The catalog minimum (name, source, license, ingestion date, schema, lineage, owner) is met only to the extent the manifest carries it; owner and quality notes are not generated.

## 6. Concrete Storage Layout

Roots come from `episteme.config.Settings` (`get_settings()`), which is the only module that reads the environment:

| Setting | Env var | Default |
|---------|---------|---------|
| `data_root` | `EPISTEME_DATA_ROOT` | `.` |
| `raw_root` | `EPISTEME_RAW_ROOT` | `<data_root>/01_raw` |
| `processed_root` | `EPISTEME_PROCESSED_ROOT` | `<data_root>/02_processed` |
| `corpus_root` | `EPISTEME_CORPUS_ROOT` | `<data_root>/03_corpus` |

```text
<data_root>/
├── 01_raw/                       # <raw_root>; per-source native downloads (e.g. pmc/oa_comm/xml, bookshelf, apollo, europepmc/...)
├── 02_processed/                 # <processed_root>
│   ├── staging/<source>/<input_key>.parquet    # (JSONL if pyarrow is missing) hand-off to the loader
│   └── _ops/
│       ├── <source>/{success,failed,load_success,graph_success}/   # per-input markers
│       ├── <source>/catalog/<source>.openmetadata.json             # manifest (section 5)
│       └── _audit/audit-YYYYMMDD.jsonl                             # audit mirror (docs/11)
└── 03_corpus/                    # <corpus_root>
    └── pretrain/source=<source>/year=<year>/part-000.parquet
```

Notes on what is and is not code-backed:

- `01_raw/` subdirectories are chosen per source by the download scripts; the tree above is illustrative, not enforced.
- `03_corpus/pretrain/` is written by `python -m episteme.data.corpus_materializer` (wrapper `scripts/data/materialize_corpus.sh`), default `--out-root` is `corpus_root`. One shard `part-000.parquet` per `(source, year)`, zstd, rows in `content_hash` order, columns `text, source, year, id, pmid, doi, license, content_hash`.
- The older layout listed `00_meta/` (licenses, inventories, checksums, notes), `04_evals/`, `05_models/`, `99_tmp/`. No code under `src/` or `scripts/` reads or writes these; they remain conventions of `docs/data-storage-details.md` only. The generated source inventory lives in `docs/12-source-inventory.md`, not `00_meta/inventories/`.

### Postgres tables (from `schema.sql` and migrations)

| Table | Partitioning | Holds |
|-------|--------------|-------|
| `episteme.articles` | `LIST (source)`; `pmc` sub-partitioned `RANGE (year)`; `DEFAULT` catch-all (`bookshelf` also has a year ladder via migration 0002) | Narrow hot row per article (no text columns) |
| `episteme.article_body` | `LIST (source)` (`pmc`, `DEFAULT`; `bookshelf` via migration 0002) | `title`, `abstract`, `body_text`, `text`; primary key `(article_id, source)` |
| `episteme.article_cites` | `HASH (src_pmid)`, 8 buckets | Citation edges `(src_pmid, dst_pmid, source_file)` |
| `episteme.article_mesh` | `HASH (pmid)`, 8 buckets | `pmid`, `descriptor_ui`, `descriptor_name`, `major_topic`, `qualifiers`, `source_file` |
| `episteme.article_parts` | `HASH`, 8 buckets (migration 0002) | Book to part edges |
| `episteme.mesh_hierarchy` | unpartitioned (migration 0003) | MeSH parent/child descriptor edges |
| `episteme.id_map` | none | PMID / PMCID / DOI cross-references |
| `episteme.chunks` | `HASH (article_id)`, 8 buckets | Phase 1 RAG scaffold; `embedding` and `chunk_tsv` need migration 0001 and pgvector / pg_search |
| `episteme._runs`, `episteme._lineage` | none, fillfactor 100 | Run and load-stage records |
| `episteme._audit` | `RANGE (recorded_at)` monthly plus `DEFAULT` | Append-only hash-chained audit trail |

## 7. Implementation Phasing (shipped state)

| Stage | Status |
|-------|--------|
| Structured outputs as Parquet with a schema | Shipped: staging shards and corpus shards. |
| Postgres store of record, idempotent loads, audit trail | Shipped (`postgres_loader`, `audit_trail`). |
| Graph property tables and edge builder | Shipped, see `docs/07-knowledge-graph-lessons.md` for which edges are built. |
| Corpus materialisation to Parquet | Shipped (`corpus_materializer`). |
| Iceberg tables | Not built, and no longer planned (ADR-0001). |
| OpenMetadata service ingest | Not built; manifest generation only. |
| pgvector / pg_search retrieval columns and indexes | Deferred: migration 0001 requires extensions absent on the local build. |
| Audit partition rotation (`create_audit_partition`) | Not built; `schema.sql` creates `_audit_202609`, `_audit_202610` and a default partition. |

## 8. Non-Negotiables

- No silent mixing of non-commercial data into commercial training data.
- No undocumented schema changes: every table change lands in `schema.sql` or a numbered migration, with an ADR-0002 addendum for new tables.
- No deletion of raw sources that still underpin loaded rows.
- License and provenance remain queryable.
- The audit trail is append-only for the application role (`docs/11-gxp-data-integrity.md`).

## 9. Doc-versus-code gaps found while rewriting

| Gap | Detail |
|-----|--------|
| ADR-0002 says `article_body` mirrors `articles` (`LIST (source)` then `RANGE (year)`) | `schema.sql` partitions `article_body` by `LIST (source)` only. |
| ADR-0002 says year sub-partitions for each source | Only `pmc` in `schema.sql`, plus `bookshelf` in migration 0002. |
| ADR-0002 names hash key `src_article_id` for the graph tables | The columns are `src_pmid` and `pmid`. |
| ADR-0002 specifies `zstd` TOAST compression | `schema.sql` tries `zstd` and falls back to `lz4` with a NOTICE; the local PG19beta3 build rejects `zstd`. |
| Migrations 0002 and 0003 create `article_parts`, `mesh_hierarchy` and the `bookshelf` partitions | `migrate_database.sh` stops at 0001 where pgvector is absent (the local build); the headers of 0002 and 0003 say to apply them directly with `psql -f`, in which case they are not tracked in `episteme._migrations`. |
| ADR-0001 says OpenMetadata ingest is opt-in via `OM_HOST` | `OM_HOST` is read into settings but unused; only the manifest file is produced. |
| ADR-0001 calls SQL/PGQ the committed primary path | See `docs/07-knowledge-graph-lessons.md` section 3.2: the graph DDL is guarded, and `graph_builder` documents that the property graph is absent on the local build so the recursive-CTE path is the one in use. |

## 10. References (internal)

- `docs/adr/0001-hybrid-storage-architecture.md`, `docs/adr/0002-data-model-storage-and-partitioning.md`
- `docs/09-extraction-contract.md` (storage, idempotency, input identity)
- `docs/11-gxp-data-integrity.md` (audit trail)
- `docs/12-source-inventory.md` (source inventory)
- `docs/05-pmc-commercial-oa.md`, `docs/06-multilingual-corpora.md`, `docs/07-knowledge-graph-lessons.md`
- `docs/data-storage-details.md` (original folder conventions, partly historical)
- `src/episteme/data/db/schema.sql`, `src/episteme/config.py`
