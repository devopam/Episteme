# Knowledge Graph Lessons for Episteme

**Source of inspiration:** [FareedKhan-dev/agentic-knowledge-graph](https://github.com/FareedKhan-dev/agentic-knowledge-graph)  
**Last updated:** August 2026  
**Status:** Principles adopted – implementation deferred to RAG / knowledge layer (Phase 1)

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

| Edge type | Source | Notes |
|-----------|--------|-------|
| `CITES` / `CITED_BY` | PubMed XML reference lists | High-value provenance |
| `ARTICLE ↔ MeSH descriptor` | PubMed MedlineCitation / MeshHeadingList | Human-curated concepts |
| `MeSH ↔ MeSH` (tree hierarchy) | MeSH RDF / XML | Enables concept expansion |
| `MeSH ↔ Qualifier` | PubMed + MeSH | Refines meaning (e.g. /drug therapy) |
| `ARTICLE → Journal / Year` | PubMed | Filtering and temporal reasoning |

Avoid early reliance on LLM-extracted `(subject, predicate, object)` triples for the core graph. Those can be added later as a secondary, lower-trust layer if needed.

### 3.2 Representation choice (early stage)

| Option | When to use |
|--------|-------------|
| **CSR (Compressed Sparse Row) in memory** | Fast neighbourhood expansion on a static graph; excellent for laptop / single-machine experiments |
| **Columnar store (Parquet) of edges** | Durable, queryable, reproducible intermediate form |
| **Graph database (Neo4j, etc.)** | Only when we need concurrent writes, complex graph query languages, or multi-user serving |

Recommended path for Episteme:

1. Parse PubMed → structured tables (articles, citations, MeSH assignments)
2. Store as Parquet (and later Iceberg)
3. Build CSR (or equivalent) views for fast traversal during experimentation
4. Consider a graph DB only when production multi-tenant RAG requires it

### 3.3 Zero-LLM graph build

The reference project built ~930M edges with **zero LLM calls** by using only NLM-published structure.

Implications for us:

- Parsing and normalisation of PubMed XML is high leverage
- Investment in robust, streaming XML → Parquet pipelines pays off more than early entity-extraction agents
- Cost and reproducibility improve dramatically

## 4. Efficient Extraction Technicalities

### 4.1 PubMed XML → structured tables

Target tables (minimum viable):

```text
articles
  - pmid (PK)
  - title
  - abstract
  - journal
  - year
  - publication_types
  - language
  - ...

citations
  - source_pmid
  - target_pmid
  - ...

mesh_assignments
  - pmid
  - descriptor_ui
  - descriptor_name
  - major_topic (bool)
  - qualifiers (list or separate table)

mesh_descriptors
  - ui (PK)
  - name
  - tree_numbers
  - ...
```

### 4.2 Parsing recommendations

- Prefer **streaming / iterative parsing** (e.g. `lxml.etree.iterparse` or equivalent) over loading entire baseline files into memory
- Process one `PubmedArticle` (or `PubmedBookArticle`) at a time
- Write row groups to Parquet incrementally
- Keep raw XML.gz untouched in `01_raw/`; all derived tables go under `02_processed/`
- Record parse version, date, and source file range in `00_meta/inventories/`

### 4.3 Performance-oriented practices

| Practice | Benefit |
|----------|---------|
| Columnar Parquet with sensible compression (ZSTD/Snappy) | Fast scans, smaller disk footprint |
| Partition by year or pmid ranges where helpful | Pruning during later queries |
| Separate “hot” fields (pmid, year, mesh uis) from long text | Better cache behaviour |
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
- Mandatory use of a heavy graph database in Phase 0
- Treating the reference project’s exact numbers or model choices as requirements

## 7. Implementation Phasing

| Phase | Focus |
|-------|--------|
| **Now (Phase 0 data)** | Robust PubMed (and later PMC) → Parquet extraction of articles, citations, MeSH |
| **Early RAG experiments** | CSR or equivalent in-memory views + hybrid retrieval |
| **Phase 1 knowledge layer** | Full provenance paths, calibrated abstention, multi-tenant RBAC over knowledge sources |
| **Later** | Optional secondary LLM-extracted edges, richer ontologies (where licensed) |

## 8. Action Items

- [ ] Design PubMed XML → Parquet schema (articles, citations, mesh_assignments)
- [ ] Implement streaming parser aligned with the above schema
- [ ] Store outputs under `02_processed/pubmed/` using the project’s storage principles
- [ ] Revisit this document when designing the first RAG prototype
