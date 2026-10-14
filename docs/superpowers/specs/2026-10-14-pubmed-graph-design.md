# PubMed graph, corrected extraction and MeSH facets — design

Date: 2026-10-14. Status: design approved by the user in chat, section by section; this spec
awaits review.

## Problem

Stage 2 loaded the full PubMed baseline (1,334 files, about 38M records) into
`episteme.articles`, and three problems showed up before `pubmed graph` ran.

- **The graph adds nothing for PubMed.** For `pubmed`, `graph_builder.build` only builds
  `article_parts` edges from `container_id`, which PubMed rows never have. `article_cites` and
  `article_mesh` are built for `pmc` only, from its JATS. PubMed's MeSH headings sit in
  `articles.mesh` as bare names. They never reach `article_mesh`, even though that table
  already has `descriptor_ui`, `major_topic` and `qualifiers` columns meant for exactly this
  data. Citations, comments and corrections, chemicals, keywords and author identifiers are
  not parsed at all. The user's view: without these links, PubMed "defeats the basic purpose
  of information amalgamation and ability to search effectively".
- **Wrong DOIs.** `extract_pubmed` walks every `ArticleId` under `PubmedData` with `iter()`.
  That includes `PubmedData/ReferenceList/Reference/ArticleIdList`, so the last cited
  reference's DOI overwrites the record's own DOI. In `pubmed26n1300`, 2,075 of 30,000 records
  (6.9%) carry a cited reference's DOI. Example: pmid 40571748's own DOI is
  `10.1007/s00059-025-05325-x`; we stored `10.1016/j.lanhl.2024.100671`. DOIs are a main join
  key to PMC, Europe PMC and OpenAlex. PMCIDs matched in that file, but the same walk puts them
  at risk.
- **Wrong retraction flag.** `is_retracted` is set when any publication type contains
  "retract". That includes "Retraction of Publication", which is the retraction notice, not a
  retracted paper.

## Goals

1. One corrected PubMed extract that fixes the DOI and retraction logic, and writes graph data
   for six relations in the same pass over the XML.
2. Load those relations into indexed graph tables with COPY, per raw file, idempotently and
   audited.
3. A MeSH facet layer that tags each record with condition, test, demographic,
   special-population and drug facets, derived from MeSH tree numbers through rules kept in a
   table, maintained incrementally.
4. Query helpers for search: reverse MeSH lookup including narrower concepts, cited-by, facets,
   ORCID.
5. A data-quality audit gate before the reload.

## Non-goals

- **Text mining of abstracts** (NER, PICO, sample sizes, conditions not indexed in MeSH). This
  is sub-project 2, with its own spec: NCBI PubTator3 as a new source first, then model-based
  extraction on rented hardware for what remains.
- **PMC and Europe PMC graph files.** The edge-file mechanism is built so Stage 3 sources can
  adopt it later; this spec changes only `pubmed` (and `mesh`, for `mesh_tree`).
- Extending the PGQ property graph `episteme_graph` to the new edge tables (deferred; the CTE
  queries cover the helpers below).
- Grants, investigators, PubMed `updates/` files (only the baseline is downloaded).

## Design

### 1. Extraction (`extract_pubmed`, schema 1.6)

The record parser is split into small functions, one per relation, each taking the
`<PubmedArticle>` element:

| Relation | Read from (and only from) | Row fields |
|---|---|---|
| ids | `PubmedData/ArticleIdList/ArticleId`, direct children only | `doi`, `pmcid` on the article row |
| mesh | `MedlineCitation/MeshHeadingList/MeshHeading` | pmid, descriptor_ui, descriptor_name, major_topic, qualifier_uis[], qualifiers[] |
| cites | `PubmedData/ReferenceList/Reference` (nested `ReferenceList`s included) | src_pmid, dst_pmid (nullable), dst_doi, ref_text |
| comments | `MedlineCitation/CommentsCorrectionsList/CommentsCorrections` | pmid, ref_type (the `RefType` attribute), ref_pmid (nullable), ref_text |
| chemicals | `MedlineCitation/ChemicalList/Chemical` | pmid, substance_ui, name, registry_number |
| keywords | `MedlineCitation/KeywordList/Keyword` | pmid, keyword, owner (the list's `Owner`), major_topic |
| authors | `MedlineCitation/Article/AuthorList/Author` | pmid, position (1-based), last_name, fore_name, initials, collective_name, orcid, affiliations[] |

Rules:

- `major_topic` for a heading is true when the descriptor or any qualifier has
  `MajorTopicYN="Y"`. The qualifier-level flags are not kept.
- ORCID: `Identifier Source="ORCID"`. It is normalised to `0000-0000-0000-000X`, taken from a
  URL form when present. A value that does not match the pattern after normalisation is
  dropped and counted, never stored malformed.
- `is_retracted` is true only when a publication type is exactly "Retracted Publication" or a
  `CommentsCorrections RefType="RetractionIn"` exists.
- Every text element of a list column passes through `cap_list_elements` (2,000 bytes).
  `ref_text`, `affiliations` elements and `keyword` use the same cap.
- `SCHEMA_VERSION` becomes 1.6, with a changelog line.

**Output.** Each raw file still writes `staging/pubmed/<input_key>.parquet`, plus one file per
relation at `staging/pubmed_graph/<relation>/<input_key>.parquet`. A relation file is written
even when it has zero rows, so "no edges" is distinguishable from "not extracted". Every
relation row carries `source_file` = the file's `input_key`.

**Atomicity.** Each output is written to a temporary name and renamed into place. The existing
success marker is written last and records `graph_relations` (the relation names) and
`graph_sha256` (one hash over the relation files in a fixed order). A file with no marker, or a
marker without `graph_relations`, is redone in full on the next run. This keeps the project's
restartability and `input_key` collision rules.

Workers: unchanged (`run_per_file`, `EPISTEME_EXTRACT_WORKERS`, default 3).

### 2. Tables (migration `0006_pubmed_graph.sql`)

One migration, wrapped in `BEGIN; … COMMIT;`, applied with MCPg `run_ddl` (structural DDL). Its
REVOKEs are run by the operator, as for 0005.

| Table | Change | Partitioning | Secondary indexes |
|---|---|---|---|
| `article_mesh` | add `qualifier_uis text[]` | hash(pmid), existing | `(pmid)`, `(descriptor_ui)` |
| `article_cites` | add `dst_doi text`, `ref_text text`; `dst_pmid` drops NOT NULL | hash(src_pmid), existing | `(src_pmid)`, `(dst_pmid)` |
| `article_comments` | new: pmid, ref_type, ref_pmid, ref_text, source_file | hash(pmid), 8 | `(pmid)`, `(ref_pmid)` |
| `article_chemicals` | new: pmid, substance_ui, name, registry_number, source_file | hash(pmid), 8 | `(pmid)`, `(substance_ui)` |
| `article_keywords` | new: pmid, keyword, owner, major_topic, source_file | hash(pmid), 8 | `(pmid)`, `(lower(keyword))` |
| `article_authors` | new: pmid, position, last_name, fore_name, initials, collective_name, orcid, affiliations text[], source_file | hash(pmid), 8 | `(pmid)`, `(orcid) WHERE orcid IS NOT NULL` |
| `article_facets` | new: pmid, facet, descriptor_ui, rule_version, source_file | hash(pmid), 8 | `(pmid)`, `(facet, descriptor_ui)` |
| `mesh_tree` | new: descriptor_ui, tree_number, source_file | none (about 60k rows) | `(tree_number text_pattern_ops)`, `(descriptor_ui)` |
| `mesh_facet_rule` | new: facet, kind (`tree_prefix` or `descriptor`), value, rule_version | none | none |
| `_graph_lineage` | new: source, source_file, graph_sha256, rows (jsonb, per relation), run_id, graphed_at; primary key (source, source_file) | none | none |

- Every edge table, and `article_facets`, gets a permanent `(source_file)` index. It is
  outside bulk mode's drop list, so per-file deletes stay cheap.
- `mesh_facet_rule` is seeded by the migration with rule_version 1:
  - **condition:** tree prefixes `C`, `F03`;
  - **test:** `E01`, `E05`;
  - **demographic:** the check-tag descriptors Humans (D006801), Animals (D000818), Male
    (D008297) and Female (D005260), and the tree prefixes `M01.060` (age groups), `M01.390`
    (Men) and `M01.975` (Women);
  - **special_population:** the five descriptors Pregnancy, Pregnant People, Infant Newborn,
    Infant Premature and Aged 80 and over, and the specific `M01` population-group prefixes
    seeded in migration 0006 (`M01.066`, `M01.150`, `M01.169`, `M01.189`, `M01.270`,
    `M01.325`, `M01.385`, `M01.686`, `M01.729`, `M01.755`, `M01.873`, `M01.920`, `M01.925`,
    `M01.930`); not every `M01` group other than `M01.060`;
  - **drug:** tree prefix `D`.

  The seed lists descriptor UIs, not names. The fixtures hold no real MeSH, so the check of the
  seed UIs against the production `mesh` rows is a read-only rollout step (Rollout step 8), not
  a pg test.
- **Privileges.** The pipeline writes as `episteme_app` (the article loader, the `pmc` graph and
  `_lineage` all do), so the new data tables and `_graph_lineage` keep `schema.sql`'s standard
  grants, like `articles` and `_lineage`. The only exception is `mesh_facet_rule`: the migration
  REVOKEs INSERT, UPDATE, DELETE and TRUNCATE on it from `episteme_app`, so only
  `episteme_sys_admin` edits facet rules. (Corrected while planning: an earlier draft said
  "SELECT only on every new table", which would have stopped the graph load itself.)

### 3. Graph load (`run_pipeline.sh pubmed graph`)

`graph_builder.build(source="pubmed")` gains a graph-file phase. The old pubmed path, which
built only `article_parts`, is replaced by it.

- **Per raw file, one transaction.** Steps:
  1. If the file was graphed before (it has a `_graph_lineage` row), delete its rows from the
     six edge tables and `article_facets` by `source_file`. On the first build there is no row,
     so no delete runs.
  2. COPY each relation file in batches, column-wise, using the loader's Arrow-batch path.
  3. Insert that file's facets: `article_facets` from that file's `article_mesh` rows joined to
     `mesh_tree` and `mesh_facet_rule`, plus `drug` facets from `article_chemicals` whose
     `substance_ui` is a MeSH descriptor in tree `D`.
  4. Write one `graph_commit` audit row with per-relation counts in its details, and upsert the
     file's `_graph_lineage` row.

  A failed file rolls back alone. The run continues with the other files and exits non-zero at
  the end with the failed count.
- **Skip unchanged.** `_graph_lineage` stores the `graph_sha256` each file was loaded from. A
  re-run skips files whose hash is unchanged; `--force --reason` overrides and is audited
  (`force_override`). The state lives in the database the deletes act on, never in a
  filesystem marker that could disagree with it.
- **Old shards are refused.** A file whose marker has no `graph_relations` fails with
  `re-extract with schema >= 1.6`.
- **`--bulk`** is allowed on `graph` as well as `load`. `bulk_indexes` is generalised from the
  six `articles` indexes to named index groups:
  - `articles` keeps its six;
  - `pubmed_graph` covers the edge and facet tables' secondary indexes, except `(source_file)`.

  The drop, rebuild, audit and status behaviour is unchanged, and the `ON ONLY` rewrite applies
  to any partitioned parent in the group.
- **`mesh graph`** additionally fills `mesh_tree`, with delete and insert by `source_file`, from
  the `TreeNumberList` it already parses for `mesh_hierarchy`. It must run before `pubmed graph`.
  `pubmed graph` refuses to start when `mesh_tree` is empty.
- **Facet rebuild.** `python -m episteme.data.graph_builder facets --rebuild [--force --reason]`
  recomputes `article_facets` one source file per transaction. It is restartable (a file
  whose facets already carry the newest `rule_version` is skipped), with one audit row per
  file. It is needed after a rule change (`rule_version` bump)
  or a new MeSH release. `facets --status` reports rows whose `rule_version` is older than the
  newest rule.

### 4. Query helpers (`graph_builder`)

- `neighbours(conn, pmid, hops, kind)` keeps `mesh`, `cites`, `part`, `mesh_parent` and
  `mesh_child`, and adds:
  - `cited_by`: who cites the pmid;
  - `comments`: `ref_type:ref_pmid` pairs, with an optional `ref_type` filter;
  - `chemical`: substance UIs;
  - `keyword`: lower-cased keywords;
  - `author`: `position:name:orcid`;
  - `facet`: `facet:descriptor_ui`.

  Rows from `pmc` and `pubmed` for the same pmid are both used, with duplicates removed.
- `articles_for_mesh(conn, descriptor_ui, include_descendants=True, major_only=False, limit)`:
  takes every tree number of the descriptor, prefix-matches `mesh_tree` for the descendants,
  then looks up `article_mesh` by `descriptor_ui`.
- `articles_for_facet(conn, facet, descriptor_ui=None, limit)` and
  `articles_by_orcid(conn, orcid, limit)`.

### 5. Data-quality audit

The scratchpad `pubmed_audit.py` becomes `episteme.data.audit.pubmed_audit`. It is read-only and
uses DuckDB over the shards, with no database access. It writes a JSON and a Markdown report
under `<processed>/_ops/pubmed/audit/`. Checks:

- **Completeness:** null rates and `extract_status`.
- **Validity:** year range, pmid, PMCID and DOI shapes, languages, licences.
- **Uniqueness:** repeated ids, DOIs shared between records.
- **Consistency:** id vs pmid, hash vs text, retraction flags vs `RetractionIn`.
- **Graph files:**
  - rows per relation;
  - share of MeSH UIs present in the `mesh` source's staging shards;
  - share of citations whose `dst_pmid` is a PubMed id in the shards;
  - ORCID check-digit validity;
  - the DOI probe: a sample of raw files re-read for their own `ArticleIdList`, with zero
    mismatches expected.

### 6. Documentation

- docs/09: schema 1.6, the graph-file layout and relation row shapes.
- docs/10: `graph --bulk`, the `mesh` → `pubmed` graph order, `facets --rebuild` and `--status`,
  the audit command.
- docs/12: pubmed graph coverage.
- The drift log.

## Error handling

| Situation | Behaviour |
|---|---|
| Extract crashes mid-file | No marker is written, so the file is redone in full on the next run. |
| Malformed ORCID or overlong text | Dropped or capped and counted in the extract summary; never stored raw. |
| A graph file fails to load | Its transaction rolls back; other files continue; the run exits non-zero. |
| Shards without graph files | That file is refused with the re-extract message. |
| `mesh_tree` is empty | `pubmed graph` refuses to start. |
| A rebuild is pending from an earlier `--bulk` | Drop refuses (exit 2), as today. |

## Testing

- **Unit tests per parser, on fixture XML:**
  - a `ReferenceList` whose references have their own `ArticleIdList`s (the DOI regression);
  - nested reference lists;
  - a reference without a PMID;
  - `RetractionIn` vs "Retraction of Publication";
  - a `CollectiveName` author;
  - ORCID as a URL, as a bare id, and malformed;
  - qualifiers with major-topic flags;
  - NOTNLM keywords;
  - a chemical without a registry number.
- **Extract:** the graph files are written with zero-row files; markers carry the relations and
  hash; a crash before the marker means a redo; the `input_key` collision test for graph files;
  shards are identical with 1 and 3 workers.
- **`pg` tests on `episteme_test`,** only through the `pg_conn` fixture:
  - 0006 applied twice, including its REVOKEs;
  - COPY load, replace on re-run, the first-run skip of the delete;
  - skip on an unchanged hash, `--force` audited;
  - old-shard refusal; the empty-`mesh_tree` refusal;
  - per-file facets, `facets --rebuild` and `--status`;
  - every new helper, including descendant lookup;
  - `bulk_indexes` groups (drop, rebuild, status for `pubmed_graph`);
  - the migration seeds 31 rules (2 + 2 + 1 + 7 + 19 across the five facets).
- **No-DB tests:** the dispatcher accepts `graph --bulk`; the audit module runs on fixture
  shards.

## Rollout

One big process at a time, in this order:

1. The running `stage2-r5` index rebuild finishes. `--bulk` needs the indexes present so it can
   drop them.
2. Run the current audit on the existing shards. Any further extractor defect it finds is fixed
   in this branch, before the re-extract.
3. Plan, then subagent-driven development, then PR; the user merges. Apply migration 0006
   through MCPg; the operator runs the REVOKEs.
4. Trial run on `pubmed26n1300`: inspect the graph files and the DOI fix; measure extract and
   graph-load speed; refine the estimates.
5. Re-extract all 1,334 files with `--force` (about 3 hours with 3 workers).
6. Audit v2. **Stop here for the user's review before loading.**
7. `pubmed load --bulk --force --reason "schema 1.6: DOI and retraction fixes"`. Estimated
   10–14 hours; the trial in step 4 will refine this.
8. `mesh graph` (fills `mesh_tree`), then check the seed descriptor UIs of
   `mesh_facet_rule` against the production `mesh` rows (read-only), then `pubmed graph --bulk`.
9. Verify:
   - the audit chain;
   - that every index is valid and has its audit row;
   - `neighbours` for each kind, `articles_for_mesh` with descendants, facet counts per facet;
   - record the measurements in the drift log.
