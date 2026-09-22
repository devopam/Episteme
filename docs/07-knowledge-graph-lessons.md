# Knowledge Graph Lessons for Episteme

**Source of inspiration:** [FareedKhan-dev/agentic-knowledge-graph](https://github.com/FareedKhan-dev/agentic-knowledge-graph)
**Last updated:** September 2026 (SP5)
**Status:** Principles adopted; the Postgres property-table graph is partly built (section 7). Storage layer superseded by ADR-0001; this document keeps the thesis and edge priorities.

## 1. Purpose

This document captures the technical and architectural lessons from the agentic-knowledge-graph project that are relevant to Episteme. We do not copy their code. We extract the ideas that improve:

- Provenance and trust
- Cost and efficiency of extraction
- Retrieval quality
- Safe refusal behaviour

These lessons primarily inform our future RAG and knowledge-layer design, while also guiding how we structure PubMed and related metadata today.

## 2. Core Thesis We Adopt

> Prefer **structured, published metadata** over **LLM-extracted entities and relations** whenever the metadata already exists and is authoritative.

For biomedical literature this means:

- Citation links (who cites whom)
- MeSH descriptors and qualifiers (human-assigned indexing)
- Journal, year, PMID, and other NLM-published fields

Every edge built this way is a **citable fact**, not a model assertion. This is especially important in healthcare and pharma contexts.

## 3. Graph Construction Principles

### 3.1 Edge types worth prioritising

| Edge type | Source | Notes | Shipped state |
|-----------|--------|-------|---------------|
| `CITES` / `CITED_BY` | Reference lists | High-value provenance | Built for `pmc` only: `<pub-id pub-id-type="pmid">` under `<ref-list>` in PMC JATS -> `article_cites`. **Not built for PubMed** (`graph_builder` handles `pmid` edges only for `pmc`). `CITED_BY` is the reverse lookup, not a stored edge. |
| `ARTICLE <-> MeSH descriptor` | PubMed MedlineCitation / MeshHeadingList | Human-curated concepts | **Partial.** `article_mesh` is filled for `pmc` from author `<kwd>` keywords (PMC JATS carries no MeSH), with `descriptor_ui` NULL. The PubMed extractor stores descriptor names in the `articles.mesh` array but nothing populates `article_mesh` from PubMed, so true MeSH article edges are unbuilt. |
| `MeSH <-> MeSH` (tree hierarchy) | MeSH descriptor XML | Enables concept expansion | Built: `mesh_hierarchy` from tree-number prefixes (`neighbours` kinds `mesh_parent`, `mesh_child`). |
| `MeSH <-> Qualifier` | PubMed + MeSH | Refines meaning (e.g. /drug therapy) | Unbuilt: `article_mesh.qualifiers` and `major_topic` exist as columns but are always NULL. |
| `ARTICLE -> Journal / Year` | PubMed | Filtering and temporal reasoning | Not edges: `journal` and `year` are columns on `episteme.articles` (year also partitions it). |
| `BOOK -> PART` | Bookshelf and other container sources | Added in SP2 | Built: `article_parts` (`neighbours` kind `part`). |

Avoid early reliance on LLM-extracted `(subject, predicate, object)` triples for the core graph. Those can be added later as a secondary, lower-trust layer if needed.

### 3.2 Representation choice (shipped)

**Decision:** the graph lives in Postgres as **property tables**, with a committed **SQL/PGQ** property-graph definition over them (ADR-0001, ADR-0002). There is no separate graph database and no Parquet edge store.

| Piece | Where | Status |
|-------|-------|--------|
| `episteme.article_cites (src_pmid, dst_pmid, source_file)` | `src/episteme/data/db/schema.sql`, `HASH (src_pmid)` x8 | Built for `pmc` |
| `episteme.article_mesh (pmid, descriptor_ui, descriptor_name, major_topic, qualifiers, source_file)` | `schema.sql`, `HASH (pmid)` x8 | Built for `pmc`, from keywords only (see 3.4) |
| `episteme.article_parts (container_id, part_id, source_file)` | migration `0002_container_and_book_parts.sql` | Built for the SP2 literature sources |
| `episteme.mesh_hierarchy (parent_descriptor_ui, child_descriptor_ui, source_file)` | migration `0003_mesh_hierarchy.sql` | Built for `mesh` |
| `CREATE PROPERTY GRAPH episteme_graph` | `schema.sql` | Guarded; may not exist (below) |
| `graph_builder.neighbours(...)` | `src/episteme/data/graph_builder.py` | Recursive-CTE path is the default; SQL/PGQ path only when the graph exists |

**The SQL/PGQ definition is guarded, and this matters.** In `schema.sql` the `CREATE PROPERTY GRAPH` statement is committed verbatim inside a `DO` block that runs it through `EXECUTE` and catches `syntax_error` and `feature_not_supported`, emitting `NOTICE: SQL/PGQ unavailable - graph_builder uses the CTE path` instead of failing the file. The schema comments and the `graph_builder` docstring record that on the local PostgreSQL 19 beta 3 build the statement is rejected with a syntax error (the schema-qualified names in its `REFERENCES` clauses), so `episteme_graph` is **not** created there. Consequences, stated plainly:

- On that build, `neighbours(kind="cites")` is answered by a recursive CTE over `article_cites`. `graph_builder._pgq_available` probes `pg_class` for `episteme_graph` and only then routes `cites` through `GRAPH_TABLE`. The PGQ back end is code-complete but unverified against real `GRAPH_TABLE` grammar; the test `test_pgq_path_when_available` skips when the graph is absent. ADR-0001 calls PGQ the "primary path" and the CTE a fallback; in practice on this build it is the reverse.
- The graph definition itself has known placeholders: the `article_mesh` edge's destination references `articles (pmid)` with `descriptor_ui` (a descriptor is not an article; the schema comment marks it "refine"), the vertex key is `id` while edge references use `pmid` (not unique in `articles`), and `mesh_hierarchy` is not part of it. `article_parts` is referenced in the graph but is created only by migration 0002, after `schema.sql`.
- Only `syntax_error` and `feature_not_supported` are caught. If a future build accepted the grammar, the missing `article_parts` table (undefined table) would not be caught and would abort `schema.sql`. This has not been exercised; it is a risk to check when the build changes.

Why property tables rather than an in-memory CSR or a graph DB: they keep citations and MeSH next to the article metadata (joinable, transactional, audited, partitioned), and traversal at the depth we use (1 to a few hops) is served by a recursive CTE. A dedicated graph database, or a CSR built for experiment speed, remains an option if multi-hop serving load demands it; neither is built.

### 3.3 Zero-LLM graph build

The reference project built ~930M edges with **zero LLM calls** by using only NLM-published structure.

Implications for us:

- Parsing and normalisation of PubMed XML is high leverage
- Investment in robust, streaming XML → Parquet pipelines pays off more than early entity-extraction agents
- Cost and reproducibility improve dramatically

## 4. Efficient Extraction Technicalities

### 4.1 Target tables (shipped names)

The originally proposed Parquet tables map onto Postgres like this:

| Proposed | Shipped |
|----------|---------|
| `articles` (pmid, title, abstract, journal, year, ...) | `episteme.articles` (narrow) + `episteme.article_body` (title, abstract, body_text, text), see `docs/09-extraction-contract.md` |
| `citations` | `episteme.article_cites` |
| `mesh_assignments` | `episteme.article_mesh` (columns as in section 3.2) |
| `mesh_descriptors` | Rows of `episteme.articles` with `source = 'mesh'`, plus `episteme.mesh_hierarchy` for the tree. Tree numbers are re-parsed from the raw descriptor XML at graph-build time and are not stored as a column. |

### 4.2 Parsing recommendations

- Prefer **streaming / iterative parsing** (e.g. `lxml.etree.iterparse` or equivalent) over loading entire baseline files into memory
- Process one `PubmedArticle` (or `PubmedBookArticle`) at a time
- Write row groups to Parquet incrementally
- Keep raw XML.gz untouched in `01_raw/`; extraction writes Parquet shards under `02_processed/staging/<source>/` and loads Postgres
- Parse version, run and input identity are recorded in `_ops` markers, `episteme._runs` / `_lineage` and the audit trail (not in `00_meta/inventories/`, which no code writes)

### 4.3 Performance-oriented practices

| Practice | Benefit |
|----------|---------|
| Columnar Parquet with sensible compression (ZSTD) for corpus shards | Fast scans, smaller disk footprint (shipped) |
| Partition by source and year, hash-partition the edge tables | Pruning during later queries (shipped, ADR-0002) |
| Separate “hot” fields (pmid, year, mesh) from long text | Shipped as `articles` vs `article_body` |
| Exact dense retrieval at moderate scale before introducing ANN | Removes recall confounds during evaluation |
| Cross-encoder rerank on a small candidate pool | High precision with limited cost |

### 4.4 Retrieval stack pattern (for later RAG)

A strong pattern observed:

1. **Hybrid grounding** of the query → MeSH / concepts (lexical + dense)
2. **Dense retrieval** over title+abstract (or full text later)
3. **Cross-encoder rerank** on a small pool (e.g. top 16)
4. **Constrained generation** (or calibrated abstention)
5. **Source-path resolution** via the graph (citations / MeSH) for provenance

This separates “finding the right evidence” from “deciding and explaining”.

## 5. Governance and Safety Lessons

| Lesson | Application to Episteme |
|--------|-------------------------|
| Every answer should carry a **source path** | Aligns with our grounding and citation goals |
| **Calibrated abstention** when confidence is low | Supports safe refusal behaviour |
| Report negative results at the same prominence as positive ones | Evaluation culture for Phase 0 and beyond |
| Do not let the model answer when the graph / retrieval cannot support the claim | Critical for clinical and regulatory contexts |

## 6. What We Explicitly Do *Not* Adopt Yet

- Full replacement of parametric LLM knowledge by the graph
- LLM-based relation extraction as the primary graph construction method
- A separate graph database (the graph is Postgres property tables, section 3.2)
- Treating the reference project’s exact numbers or model choices as requirements

## 7. Implementation Phasing (shipped state)

| Item | State |
|------|-------|
| PubMed / PMC extraction to normalised rows, loaded to Postgres | Shipped (`docs/09`, `docs/12`) |
| `article_cites` and keyword-derived `article_mesh` for `pmc` | Shipped (`graph_builder`, CLI `python -m episteme.data.graph_builder --source pmc`) |
| `article_parts` for SP2 literature sources; `mesh_hierarchy` for `mesh` | Shipped |
| Committed SQL/PGQ property graph | Committed but guarded; not created on the local PG19beta3 build (section 3.2) |
| Citation edges for PubMed; true MeSH article edges; qualifiers and major-topic flags | **Unbuilt** |
| In-memory CSR views | Unbuilt; not needed at current traversal depth |
| Hybrid retrieval, cross-encoder rerank, calibrated abstention, source-path resolution | Unbuilt (Phase 1); `chunks` is a scaffold and its vector / BM25 columns await migration 0001 and the extensions |
| Optional secondary LLM-extracted edges, richer ontologies | Later |

## 8. Action Items

- [x] Define the PubMed/PMC schema (`episteme.articles`, `article_body`, `article_cites`, `article_mesh`, `article_parts`, `mesh_hierarchy`)
- [x] Streaming extraction aligned with that schema, output to `02_processed/staging/`, loaded into Postgres
- [ ] Populate `article_mesh` from PubMed `MeshHeadingList` (descriptor UI, major topic, qualifiers) instead of, or alongside, PMC keywords
- [ ] Build `article_cites` for PubMed (no reference-list extraction exists for it today)
- [ ] Refine the `episteme_graph` definition (MeSH edge destination, unique vertex key, `mesh_hierarchy`) and verify `GRAPH_TABLE` grammar on a build that accepts it
- [ ] Revisit this document when designing the first RAG prototype
