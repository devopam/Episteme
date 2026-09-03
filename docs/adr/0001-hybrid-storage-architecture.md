# ADR-0001: Hybrid Storage Architecture

**Date:** 2026-09-03  
**Status:** Accepted  
**Supersedes:** storage layer decisions in `docs/07-knowledge-graph-lessons.md`, `docs/08-data-storage-principles.md`

---

## Context

The original design (`docs/07` and `docs/08`) mandated Apache Iceberg as the processed layer store, with OpenMetadata (file-system-backed) as the catalog and lineage layer. This choice optimized for data versioning and were-provenance at the cost of architectural simplicity.

However, the user selected **PostgreSQL 19** as the primary datastore for Phase 0, driven by three concurrent needs:
1. A **graph engine** to run citation (`article_cites`) and Medical Subject Headings (`article_mesh`) property tables natively — SQL:2023's `PROPERTY GRAPH` and `GRAPH_TABLE`/`MATCH` queries are committed in `db/schema.sql`.
2. A **RAG and lexical-search foundation** for Stream 2 (Phase 1) — `pgvector` (dense embeddings) and `pg_search` (BM25 full-text indexing) as native extensions.
3. **Transactional, idempotent data loading** — per-source loading as delete-by-`source_file` + append, coupled to audit-trail recording in the same transaction.

`docs/02-data-sources.md` already anticipated Postgres for Stream 2's knowledge and retrieval layers. This decision consolidates that into the Phase-0 schema and confirms Postgres as the single primary OLTP store.

## Decision

**Storage model: Postgres-centric hybrid.**

| Layer | Store | Holds | Notes |
|---|---|---|---|
| `01_raw/` | Filesystem | Native downloads (XML.gz, JSON, tarballs) | Immutable, per-source, never deleted |
| `02_processed/staging/` | Parquet shards | Normalised `episteme.articles` rows, one shard per input file | Transient staging; consumed by `load_articles` |
| **Postgres `episteme` schema** | PostgreSQL 19 | Structured `articles`, `article_body`, graph property tables (`article_cites`, `article_mesh`), `chunks` (Phase 1 scaffold), `id_map`, `_runs`, `_lineage`, `_audit` (append-only, hash-chained) | Primary OLTP store; home for the graph engine and RAG indices. Transactional load + audit trail coupling. |
| `03_corpus/pretrain/` | Parquet shards | Materialised flat-`text` training corpus, filtered (`subset='commercial' AND extract_status='ok'`) | Snapshot per training run; fast load for ML frameworks (`datasets`, `polars`). |
| **OpenMetadata** (Postgres-backed) | Service | Catalog, lineage, data contracts over `episteme` schema and `03_corpus/` | Metadata ingest is opt-in via `OM_HOST` env var. Manifest generated even if OpenMetadata service is absent. |

**Graph engine:** SQL/PGQ (`CREATE PROPERTY GRAPH` DDL, `GRAPH_TABLE`/`MATCH` queries) is the committed primary path. Property tables (`article_cites`, `article_mesh`) are the underlying structure. Recursive-CTE fallbacks are reserved for CI/degraded environments lacking PGQ support; they are **not** the design target.

**Extensions:** `vector` (pgvector, dense embeddings) and `pg_search` (BM25 lexical search) are enabled from the start, guarded with `IF NOT EXISTS` so missing extensions degrade to warnings, not failures. Both are unused in Phase 0 but reserved for Phase 1.

## Consequences

- **Training corpus throughput:** Parquet materialization and columnar format enable rapid streaming reads by `datasets`, `polars`, and TensorFlow's loading pipelines — orders of magnitude faster than row-at-a-time reads from Postgres.
- **Graph co-location:** The citation and MeSH networks live in the same database as the article metadata, eliminating a separate graph service and its operational burden. Queries can seamlessly `JOIN` metadata with graph traversals.
- **Single operational database:** One Postgres instance scales to serve OLTP loads, graph traversals, and RAG index reads. Deployment, backup, and access control are simplified.
- **Audit trail integration:** Data changes and audit records are written in the same transaction, guaranteeing consistency and making tampering detectable via hash-chain inspection.
- **Superseded:** The "processed layer = Apache Iceberg" clause of `docs/08-data-storage-principles.md` is no longer authoritative. SP5 will rewrite `docs/08` to reflect this architecture.

## Alternatives Considered

1. **All-Iceberg (original design).**
   - **Rejected:** Iceberg excels at ACID transactions on the data lake, but provides no native graph query engine. A separate property-graph database (Postgres, Neo4j, JanusGraph) would still be required, recreating the operational burden we sought to avoid. Iceberg's version snapshots are valuable for audit trails but can be replicated via Postgres WAL archiving and the tamper-evident audit-trail schema.

2. **All-Postgres, including bulk article text in the `articles` table.**
   - **Rejected:** While simpler in principle, embedding `article_body.text` (tens to hundreds of KB per row) in the hot `articles` table defeats the purpose of a "narrow hot table" optimized for index scans and joins. Row-store-at-scale tuning becomes a burden; TOAST compression helps but cannot eliminate the fact that every full-table scan touches vast amounts of cold data. Training corpus materialization still requires extracting the `text` column into Parquet for throughput. Splitting `article_body` as a separate table, indexed by `article_id`, lets the hot table stay cache-resident and query-efficient.

