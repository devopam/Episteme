# Data Storage Principles – Episteme

**Last updated:** August 2026  
**Status:** Binding for Phase 0 onward

## 1. Decision

From the beginning, Episteme standardises on:

| Layer | Technology | Role |
|-------|------------|------|
| **File format** | Apache Parquet | Physical storage of columnar data |
| **Table format** | **Apache Iceberg** | ACID tables, schema evolution, time travel, multi-engine access |
| **Metadata & governance** | **OpenMetadata** | Catalog, lineage, ownership, license tracking, discoverability |

Raw downloads may remain in their native form (XML.gz, etc.).  
All **processed and curated** datasets should move toward Iceberg tables backed by Parquet, with metadata registered in OpenMetadata.

## 2. Goals

- Avoid proprietary lock-in
- Support reproducibility and auditability
- Enable multiple engines (Spark, Trino, DuckDB, Polars, etc.) on the same data
- Make license, provenance, and lineage first-class
- Scale from laptop experiments to larger cloud training/RAG workloads without redesign

## 3. Layered Storage Model

```text
01_raw/          → Original downloads (XML, JSON, zip, etc.) – immutable
02_processed/    → Cleaned, normalised data as Parquet / Iceberg tables
03_corpus/       → Training-ready mixtures (Iceberg or versioned Parquet)
00_meta/         → Inventories, checksums, license records
OpenMetadata     → Catalog + lineage over the above
```

### Rules

1. **Raw is sacred** – never overwrite or “fix in place” under `01_raw/`.
2. **Processed is derived** – always reproducible from raw + code version.
3. **Corpus is curated** – explicit mixtures with recorded recipes and sampling ratios.
4. **Metadata is mandatory** – every significant dataset should be visible in OpenMetadata with license and provenance.

## 4. Apache Iceberg – How We Use It

### 4.1 Why Iceberg

- Open specification
- Hidden partitioning and partition evolution
- Schema evolution without rewriting entire datasets
- Time travel / snapshot isolation
- Compatible with many query engines

### 4.2 Practical conventions

| Topic | Convention |
|-------|------------|
| Catalog | Start simple (Hadoop / REST / JDBC catalog); move to a proper catalog service as needed |
| File format underneath | Parquet (ZSTD or Snappy) |
| Naming | `episteme.<domain>.<entity>` (e.g. `episteme.pubmed.articles`) |
| Partitioning | Prefer hidden partitioning; common keys: `year`, `source`, `language` |
| Snapshots | Retain enough history for reproducibility of training runs |
| Writes | Prefer append + controlled compaction; avoid unnecessary small files |

### 4.3 Early-stage simplification

While the project is still small:

- It is acceptable to write **plain Parquet** first
- Promote stable datasets to **Iceberg tables** as soon as multiple tools or concurrent access appear
- Do not delay useful work only to set up a perfect catalog on day one

The *direction* is Iceberg from the start; the *rollout* can be incremental.

## 5. OpenMetadata – How We Use It

### 5.1 Why OpenMetadata

- Open metadata standard and schemas
- Strong support for data assets, lineage, ownership, and glossaries
- Fits a multi-source biomedical data platform
- Helps enforce license and provenance visibility

### 5.2 Minimum metadata we record per dataset

- Name and description
- Source system / URL
- License (and commercial-use status)
- Ingestion date and code/version used
- Schema (columns, types)
- Lineage (raw → processed → corpus)
- Owner / maintainer
- Quality notes or known limitations

### 5.3 Integration points

- Register major Iceberg / Parquet datasets after each significant pipeline run
- Link license files under `00_meta/licenses/` to the corresponding assets
- Use tags for: `commercial-ok`, `research-only`, `language:*`, `source:pubmed`, etc.

## 6. Concrete Storage Layout (aligned with existing structure)

```text
/EpistemeData/
├── 00_meta/
│   ├── licenses/
│   ├── inventories/
│   ├── checksums/
│   └── notes/
├── 01_raw/                    # Native formats only
├── 02_processed/              # Parquet → Iceberg tables
│   ├── pubmed/
│   ├── pmc_comm/
│   ├── multilingual/
│   └── ...
├── 03_corpus/                 # Training mixtures (Iceberg preferred)
├── 04_evals/
├── 05_models/
└── 99_tmp/
```

Iceberg warehouse path (example):

```text
s3://... or /EpistemeData/warehouse/episteme/...
```

(Exact warehouse location can be local on the SSD first, then cloud.)

## 7. Implementation Phasing

| Stage | Action |
|-------|--------|
| **Immediate** | Write all new structured outputs as Parquet with clear schemas |
| **Short term** | Introduce Iceberg for stable processed tables (PubMed articles, citations, MeSH, Apollo extracts) |
| **Short–medium term** | Stand up OpenMetadata and register core assets + licenses |
| **Ongoing** | Every new source follows: raw → Parquet/Iceberg → catalog entry |

## 8. Non-Negotiables

- No silent mixing of Non-Commercial data into commercial training tables
- No undocumented schema changes to published Iceberg tables
- No deletion of raw sources that still underpin any Iceberg table
- License and provenance must remain queryable

## 9. References (internal)

- `docs/05-pmc-commercial-oa.md`
- `docs/06-multilingual-corpora.md`
- `docs/07-knowledge-graph-lessons.md`
- Project storage layout: `data-storage-details.md`
