# ADR-0002: Data Model, Storage Optimisation & Partitioning

**Date:** 2026-09-03  
**Status:** Accepted  
**Binding on:** `db/schema.sql` (Task 3), `episteme.data.corpus_materializer`  
**Replaces:** deferred schema decisions in `docs/09-extraction-contract.md` §4

---

## Context

Phase 0 loads ~15 million literature records and millions of structured-data rows into Postgres for training and future RAG retrieval. Without explicit partitioning and storage strategy decided at table-creation time, the schema becomes difficult to optimize in production. The citation and MeSH graphs are large (hundreds of millions of edges), and the full-text `article_body` columns are bulky. Training corpus materialization demands fast, filtered reads.

This ADR commits the partitioning strategy, index design, and Parquet layout **now**, at schema authoring time, so that `db/schema.sql` encodes storage choices rather than defaulting to single-partition, row-store-everything approaches that would require expensive retrofits.

## Decision

### Postgres: Partitioning and Storage

**`episteme.articles` — narrow hot table, list-range partitioned.**
- **Partitioning:** `PARTITION BY LIST (source)`, each source `PARTITION BY RANGE (year)`.
- **Year buckets:** `0` (unknown year), `(-∞, 1990)`, `[1990,1995)`, `[1995,2000)`, …, `[2020,2025)`, `[2025,2030)`, `[2030,∞)`.
- **Rationale:** Near every query filters by `source` + `year`; per-source detach is cheap; per-year vacuuming is efficient. The hot table contains only narrow identifiers (`pmid`, `pmcid`, `doi`, `source`, `year`, `retrieved_at`, `content_hash`, `extract_status`, `license`, `subset`, etc.) — no large text columns.
- **Fillfactor:** Default.
- **Indexes:** partial B-tree on `pmid`, `pmcid`, `doi` (WHERE … IS NOT NULL); BRIN index on `retrieved_at` (append-ordered by ingestion); GIN on `mesh` (array of MeSH descriptors), `authors` (array), `publication_types` (array).

**`episteme.article_body` — text columns, toast-compressed, partitioned to mirror `articles`.**
- **Columns:** `article_id` (PK/FK to `articles`), `title`, `abstract`, `body_text`, `text` (concatenated for training).
- **Partitioning:** mirrors `articles` (same `LIST (source)` → `RANGE (year)` structure) to enable partition-wise joins and to localize large-column vacuuming.
- **Compression:** `toast_compression = 'zstd'` on `body_text` and `text` to reduce storage and improve retrieval-layer throughput.
- **Rationale:** Splitting text into a secondary table keeps the hot `articles` table cache-resident for index scans and metadata joins; TOASTed text is rarely scanned whole.

**`episteme.article_cites`, `episteme.article_mesh` — property-graph foundation, hash-partitioned.**
- **Partitioning:** `PARTITION BY HASH (src_article_id)` (or `pmid` equivalent) on 8 buckets.
- **Rationale:** No natural temporal or categorical range key; hash distribution ensures balanced partitions. `MATCH` graph traversals profit from partition pruning on the anchor (source) node.
- **No range pruning:** unlike `articles`, graph tables are queried by traversal starting point, not by time or category, so hash is the right strategy.

**`episteme.chunks` — Phase 1 scaffold, hash-partitioned.**
- **Partitioning:** `PARTITION BY HASH (article_id)` on 8 buckets.
- **Indexes (Phase 1):** HNSW on `embedding`, BM25 on `chunk_text` (via `pg_search`, per partition).
- **Status in Phase 0:** table structure only; no data. Reserved for vector-RAG retrieval in Phase 1.

**`episteme._audit` — append-only audit trail, range-partitioned by month.**
- **Partitioning:** `PARTITION BY RANGE (recorded_at)` with monthly boundaries (one partition per calendar month).
- **Retention:** infinite; never dropped. 30-day post-hoc compression to gzip (via `rotate_audit_logs.sh`) on read-only partitions.
- **Access control:** Application DB role granted `INSERT` + `SELECT` only — **never `UPDATE` / `DELETE`** (enforced by `GRANT` in schema). Prevents accidental tampering.
- **Rationale:** Monthly partitions let the log-rotation job detach and compress old read-only partitions while keeping the live partition small.

**`episteme._lineage`, `episteme._runs` — unpartitioned.**
- **Size:** small (one row per extraction run, one row per load stage per source).
- **Fillfactor = 100:** append-only, never updated in place; 100 fills the page to capacity on insert, reducing future vacuuming overhead.
- **Autovacuum:** tuned per large partitions (shorter `autovacuum_vacuum_cost_delay` on high-churn tables like `_audit` at month-end).
- **Monitoring:** `pg_stat_statements` enabled to track slow queries across the schema.

### Parquet: Corpus Materialization

**`03_corpus/pretrain/` — Hive-partitioned by source and year.**
- **Layout:** `source=<source_name>/year=<year>/part-<N>.parquet`.
- **File size:** 256–512 MB per part file (balances I/O parallelism and merge-on-read cost).
- **Row groups:** 128 MB, enabling column-selective decompression and skip during scanning.
- **Compression:** `zstd` level 3 (speed/compression trade-off for training throughput).
- **Dictionary encoding:** on low-cardinality columns (`source`, `license`, `subset`, `extract_status`, `language`).
- **Row sort order:** within each shard, rows sorted by `content_hash` (enables locality-of-reference for deduplication and speeds filter predicates).
- **Column order:** hot→cold (frequently scanned columns first: `text`, `title`, `abstract`, `source`, `year`, then metadata).
- **Filter in corpus job:** `subset='commercial' AND extract_status='ok'` — only include articles marked fit for pretraining.

### Standing Rule for Future Tables

**Every new table or source added to the schema MUST declare its partition key and storage strategy in a one-paragraph addendum to this ADR,** reviewed in the PR that introduces it. The addendum names the table, its partitioning logic, the rationale, and any tuning flags (`fillfactor`, compression settings, custom autovacuum). This keeps storage decisions visible and auditable.

## Consequences

- **Query performance:** Partition pruning on common filters (`source`, `year`, `article_id`) reduces full-table scans and I/O. Index selectivity improves with partition-wise aggregate pushdown.
- **Operational efficiency:** Small per-partition vacuuming (monthly for `_audit`, per-source-year for `articles`) reduces CPU load and downtime compared to whole-table maintenance.
- **Training throughput:** Parquet corpus shards are read in parallel by `polars.scan_parquet`, `datasets.load_dataset`, and TensorFlow without decompressing or materializing text in Postgres.
- **Graph scalability:** 8-way hash partitioning on graph tables spreads write load and keeps per-partition working sets small, improving cache behavior for `MATCH` traversals.
- **Audit trail integrity:** Monthly partitions + read-only `GRANT` + hash-chain verification make tampering detectable; old records are compressed but never deleted.
- **Schema complexity:** Partitioning + mirrored table structure (`articles` + `article_body`) adds schema design burden upfront but eliminates the need for expensive post-deployment refactoring.

## Alternatives Considered

1. **Single partition, row-store everything.**
   - **Rejected:** Works for 10s of millions of rows on modern hardware but requires aggressive tuning (`fillfactor`, TOAST settings, custom autovacuum) and defeats per-source / per-year vacuum efficiency. Training corpus reads would require extracting the full `text` column via SQL, not materializing to Parquet — orders of magnitude slower.

2. **Range-partitioning `articles` on `year` only (no source-level partitioning).**
   - **Rejected:** Loses the ability to detach or re-balance a source cleanly. Most queries filter `source` first; year-only partitioning causes partition pruning to fail on source-filtered scans.

3. **Hash-partitioning `articles` on `content_hash` (for near-dup pruning at query time).**
   - **Rejected:** `content_hash` is sparse (many articles have NULL); hash distribution would not align with query patterns. Deduplication is a corpus-materialization concern, not a schema concern (handled by `corpus_materializer` via MinHash LSH in the loading job).

4. **All text columns in `articles` (no secondary `article_body` table).**
   - **Rejected:** Embedding `title`, `abstract`, `body_text`, `text` (total 50–200 KB per row) in the hot table balloons index entries and cache pressure. Hot columns like `pmid` and `source` become slower to query because TOAST decompression happens incidentally.

