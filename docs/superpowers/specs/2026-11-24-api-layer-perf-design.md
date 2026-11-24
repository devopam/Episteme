# SQL lookup API: performance follow-up (addendum to 2026-11-17-graphrag-api-layer-design)

Status: owner approved the follow-up PR on 2026-11-24 ("Yes, please start on the PR"), after
the step 11 report of the 0015 rollout. This addendum changes only what it names; every other
rule of `2026-11-17-graphrag-api-layer-design.md` stays.

## Why

Step 11 of the rollout ran every public function through the reader MCPg instance
(`episteme_api_reader`, `statement_timeout` 30 s, restricted mode: the timeout cannot be raised).
Three lookups fail or come near the timeout on production:

| Call | Measured | Cause (measured on production, 2026-11-24) |
|---|---|---|
| `literature_by_mesh(60725, NULL, 2020, 2024)` (metformin) | timeout; 192 s as admin | Every hit (20,733 PMIDs) gets a year lookup on all `articles` partitions and a second `article_mesh` read before the sort and the limit. Cold, the hit step alone reads one `article_mesh` heap page per row: aspirin 50,617 hits 177 s, atorvastatin 8,021 hits 212 s, warfarin 22,355 hits 249 s. The year step, done as one `pmid = ANY(...)` probe per PubMed year partition, takes 0.8 to 1.4 s. |
| `graph_neighbours(..., 'shared_mesh')` | 0.4 to 24 s | (a) `pmcid:...` ids: `articles_default` had stale statistics (`pmid` null_frac 1.0), so the id lookup walked the whole `articles_default_pmid_idx` (1,006,167 buffers). `ANALYZE episteme.articles_default` on 2026-11-24 fixed the plan (3 ms). (b) Each seed descriptor reads up to 20,001 `article_mesh` rows twice, one heap page per row (41k pages for 3 descriptors). |
| `search_text(q, '{dailymed,ema_epar}')` | 15 s warm, timeout cold | The rank recomputes `to_tsvector` over the whole label text (mean 65,899 characters) of each match: 1,406 matches, 15.9 s. Rank on the first 10,000 characters: 2.3 s. |

## Decisions

1. **Covering indexes on `article_mesh`** (index-only scans; the visibility map is ~100 % set):
   `article_mesh_lower_name_idx` becomes `(lower(descriptor_name)) INCLUDE (descriptor_name, pmid)`
   and `article_mesh_descriptor_ui_idx` becomes `(descriptor_ui, pmid)`. Same names, so the
   `pubmed_graph` group of `bulk_indexes` (it saves and replays the catalog definition) needs no
   change. `descriptor_name` must be in the name index: an index-only scan needs every column the
   query reads. Checked on `episteme_test` with synthetic rows: `Index Only Scan`, `Heap Fetches: 0`
   for the three query shapes. On production the swap is a runbook step (build under a temporary
   name, drop the old index, rename), one heavy job at a time, after the merge.
2. **`literature_by_mesh` keeps its contract** (order `year DESC NULLS LAST, article_id`; the first
   `p_limit` rows; unknown years last and only without a year filter) and computes it exactly with
   descending **year windows**: the hit PMIDs are probed against one window of PubMed years at a
   time; after a window, the function stops when it holds at least `p_limit` rows (no later window
   can outrank them, and every tie inside the window is present). The first window is
   `literature_window` years wide (config family, default 5); each next window is twice as wide.
   The topic check runs on each window's rows, not on all hits.
3. **Year source: the `pubmed` record only.** The old rule took `max(year)` over every source with
   the PMID. Measured on the metformin hits: 209 of 20,733 differ, every sampled one because a
   `europepmc_manuscript` row carries the later deposit year (for example PMID 18695610: pubmed
   2008, manuscript 2014). The PubMed year is the publication year; the old value was a small bug.
   The same rule applies to the year of `shared_mesh` rows. A year bound applies to the largest
   `pubmed` year of the PMID, as in 0015: a PMID with a `pubmed` row above the upper bound is
   dropped (one array probe per window, on the partitions above the bound; PR #122 review).
   Years below 1 count as unknown; the open upper bound is the largest `int`.
4. **Descriptors and `source_file` are read only for the rows returned** (`literature_by_mesh`:
   the kept PMIDs; `shared_mesh`: the top rows), not for every hit.
5. **`graph_neighbours` id lookup** without `ORDER BY ... LIMIT 1`: `min(pmid COLLATE "C")` over
   the rows with `id = v_id`. This removes the `ORDER BY ... LIMIT` form that led the planner to walk the `pmid` index. Fresh statistics (`ANALYZE`) are still needed.
6. **`search_text` rank input for label sources** (`dailymed`, `ema_epar`): title, abstract and
   the first `search_rank_chars` characters (config family, default 10000) of the label text and
   of `text_override`. **The match is unchanged** (the exact index expression). This is a contract
   change: scores and the order of label results move (the top score for "lactic acidosis" went
   from 6.34 to 3.04). Other sources are unchanged.
7. **Acceptance:** after the rollout, run these calls through the reader MCPg instance
   (`scripts/mcpg-episteme.sh episteme --reader`) on a fresh connection, for substances not used
   while prototyping: `literature_by_mesh` for acetaminophen 19464, ibuprofen 149266 and
   simvastatin 69503 without a filter; one year-filtered call; one topic call; one rare-topic
   call on a large substance (for example aspirin 127871); `graph_neighbours('pmcid:<id>',
   'shared_mesh')`; `search_text` with `p_sources` `'{dailymed,ema_epar}'`. Every call must take
   less than 10 s. A call that misses is reported with its time, not hidden. After
   `VACUUM (ANALYZE) episteme.article_mesh`, `EXPLAIN (ANALYZE, BUFFERS)` of one literature window
   probe and of the `shared_mesh` hit step must show an Index Only Scan with Heap Fetches near 0.

## Not in this change (owner decisions, reported)

- `effective_io_concurrency = 2` (configuration file) and `io_workers` at its default (3) limit
  parallel reads on the E: drive (Kingston SNV3S2000G, UAS driver: the transport is correct).
  Measured cold random reads: 3.5 to 26 ms per page.
- 98 large tables were never analyzed since the statistics reset (for example `article_cites_*`,
  `article_facets_*`, `id_map`). The runbook gets an ANALYZE step; it is not run in this change.
