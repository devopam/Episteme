# SQL lookup API: no call times out (second addendum to 2026-11-17-graphrag-api-layer-design)

Status: owner direction of 2026-11-24, after the 0016 rollout report: "package significant
changes ... we would need to fix these surely ... it shouldn't time out". Sub-second answers
are not a goal. Every call must finish inside the reader timeout (30 s) and give a correct or
an explicitly partial result. This addendum changes only what it names; every other rule of
`2026-11-17-graphrag-api-layer-design.md` and `2026-11-24-api-layer-perf-design.md` stays.
Migration 0016 is applied on production, so the changes go in a new migration, **0017**.

## Why (measured on production, 2026-11-24, as admin)

| Call | Measured | Cause |
|---|---|---|
| `literature_by_mesh(127871 aspirin, 'Reye Syndrome')` | 30 s reader timeout | A rare topic never fills `p_limit`, so every window runs and the topic check reads `article_mesh` heap rows per PMID (`t.pmid = g.pmid AND (descriptor OR qualifier)`) for all 50,617 hits. Cold heap reads cost 3.5 to 26 ms each. |
| `literature_by_mesh(127871, 'supply & distribution')` (rare qualifier) | 12.4 s warm (cold: timeout) | Same per-hit heap check; `qualifiers` is in no index. |
| `search_text('liver')` (default sources) | 30.5 s: timeout | pubmed 7.1 s, openalex 7.6 s, dailymed 7.5 s, pmc 1.4 s. |
| pubmed part of it | 5.5 s of the 7.1 s is the match | `work_mem` 32 MB: the bitmap of 1,038,960 matches goes lossy (3,275 lossy blocks), so 27,243 rows get `fts_doc` recomputed. At 256 MB: exact bitmap, 0.45 s. `'patients'` (9,299,917 matches) at 256 MB: 2.8 s with disk reads. |
| dailymed part of it | 8.3 s for 5,000 matches | Rank: `to_tsvector` over 10,000 characters per label, ~0.33 µs per character (1,000 chars: 2.0 s; 2,000: 3.6 s; 4,000: 7.0 s). The match itself is 10 ms. |

Topic set forms measured on aspirin (50,617 hits):

| Form | Reye Syndrome (1 descriptor) | Hemorrhage (55) | Cardiovascular Diseases (471) | Neoplasms (703) |
|---|---|---|---|---|
| `descriptor_ui = ANY(set) AND pmid = ANY(hits)` (two arrays in the index condition) | 0.17 s | 2.3 s | 13.3 s warm | 20.5 s warm |
| pmid index per hit + heap filter (planner's hash form) | | | 1.45 s warm | 1.25 s warm, 24 s cold |
| **fenced index-only scan of the set (`LIMIT`), hash join with the hits** | **0.07 s** | **0.28 s** (atorvastatin) | **2.2 s** (warfarin, exact) | **3.0 s for 5,000,001 rows** (7.7M rows carry the set) |

## Decisions

1. **Topic kinds.** A topic is a descriptor name (as in 0015/0016), a MeSH qualifier name, both
   (for example `mortality`, `poisoning`), or neither.
   - The qualifier names come from data: new table `episteme.mesh_qualifier (qualifier_ui,
     qualifier_name, source_file)`, filled by the existing `mesh` graph phase from the
     `AllowableQualifiersList` of each descriptor in the MeSH descriptor file (DELETE by
     `source_file`, then insert; one audit row with the other mesh tables). The sample of
     `article_mesh` qualifiers holds 76 names; desc2026 is expected to give the same.
   - When `mesh_qualifier` is missing or empty, every topic counts as a possible qualifier
     (the 0016 behaviour).
   - **Neither** (and `mesh_qualifier` is filled): one note row
     `topic <t> is not a MeSH descriptor or qualifier name` and no article rows. No scan.
     (0016 scanned every hit and returned nothing.)
2. **Descriptor side of a topic: one fenced set read.** Read the PMIDs of the descriptor set
   with an index-only scan of `(descriptor_ui, pmid)`, at most `literature_topic_scan + 1`
   rows (new config family, default 3,000,000; not a row cap), and hash-join them with the
   hits. The fence (`OFFSET 0`/`LIMIT` subquery) keeps the planner off the two-array form.
   - At most `literature_topic_scan` rows: the set is exact. When the topic is not a qualifier
     name, the hits become this intersection before the year windows, so the windows read the
     years of few PMIDs and no window runs a topic check.
   - More rows (a very common topic): the set is not used. Each window checks its own rows
     with the two-array form (`descriptor_ui = ANY(set) AND pmid = ANY(window pmids)`,
     index-only). A common topic fills `p_limit` in the first windows.
3. **Qualifier side of a topic** (topic is a qualifier name, or the list is missing): rows of a
   window that the descriptor side did not keep get the 0016 heap check, in chunks of
   `literature_topic_chunk` PMIDs (new config family, default 500), newest year first. The
   time budget (decision 4) is checked between chunks.
4. **Time budget** (new config family `time_budget_ms`, default 20000): `literature_by_mesh`
   checks `clock_timestamp()` against the call start between windows and between qualifier
   chunks; `search_text` checks it before each source. When the budget is spent, the function
   stops, returns what it has (sorted, cut, as usual) and adds one note:
   - literature: `partial: time budget of <n> ms spent; years from <y> down not searched`
     (`<y>` = the newest year not fully searched);
   - search: `partial: time budget of <n> ms spent; not searched: <sources>`.
   A count cap cannot give this guarantee: the same probe costs 1.25 s warm and 24 s cold.
   The result can then depend on the cache; the note says so. 20 s leaves 10 s of the reader
   timeout for the last query.
5. **`search_text` match memory.** The function sets `work_mem` to `search_work_mem_mb` MB
   (new config family, default 256) for the call (`set_config(..., true)`; the old value is
   set back before the final query). The bitmap stays exact for a common word.
6. **`search_text` rank input (label sources only)**: title, abstract, and the first
   `search_rank_chars` characters of `body_text` and of `text_override`, for `dailymed` and
   `ema_epar` only, as in 0016. `fts_doc` reads `body_text` and `text_override` only for these
   two sources, so every other source ranks on the full `fts_doc` as before. Only the default
   changes: `search_rank_chars` goes from 10,000 to 2,000. **Contract change:** label scores
   and their order move again. Scores of `pmc`, `bookshelf` and `openalex` do not move. The
   match is unchanged (the exact index expression).
7. **Acceptance** (as 0016 decision 7, reader instance, fresh connection, substances not used
   in prototyping; aspirin is now used): `literature_by_mesh` warfarin 37523 with a rare
   descriptor topic, atorvastatin 67435 with `Neoplasms`, warfarin with the rare qualifier
   `supply & distribution`, one topic that is neither; `search_text('liver')` and
   `search_text('patients')` with default sources; the 0016 list again. Target: each call
   < 10 s; hard limit: no timeout. A call that returns a `partial:` note is reported with its
   time.
8. **Runbook:** after each index swap, `ANALYZE` the table at once (the 0016 rollout lesson: the
   planner had no statistics for the new expression index and chose a parallel sequential
   scan, 227 s). The rollout of 0017: migration, `api_settings sync`, `run_pipeline.sh mesh
   graph` (fills `mesh_qualifier`), acceptance.

## Builder's calls (not owner decisions)

- The default values of the four new families and of `search_rank_chars`.
- The time budget instead of a count cap for the per-PMID paths (decision 4).
- `work_mem` set inside the function instead of in the server configuration.
- Decision 6 was corrected during implementation: the first text said the rank input change
  applies to every source. It applies to the label sources only.
