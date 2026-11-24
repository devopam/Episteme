# API no-timeouts (0017) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No SQL lookup API call times out on the reader instance (30 s); rare and qualifier topics, and common full-text words, finish or return an explicit `partial:` note.

**Architecture:** New migration 0017 replaces the bodies of `literature_by_mesh` and `search_text` (same signatures) and adds the table `episteme.mesh_qualifier`, filled by the existing `mesh` graph phase. Four new config families plus a new `search_rank_chars` default steer the work. A time budget counted from `statement_timestamp()` stops long paths with a note.

**Tech Stack:** PostgreSQL 19beta3 (plpgsql), Python 3.13, psycopg 3, pytest (`pg` marker), defusedxml.

**Spec:** `docs/superpowers/specs/2026-11-24-api-no-timeouts-design.md` (second addendum to `2026-11-17-graphrag-api-layer-design.md`; first addendum `2026-11-24-api-layer-perf-design.md`).

## Global Constraints

- Migration file: `src/episteme/data/db/migrations/0017_api_no_timeouts.sql`; idempotent; one transaction (`BEGIN; ... COMMIT;`); applied after 0016 as `episteme_sys_admin`. Do not edit 0016.
- `src/episteme/data/db/schema.sql` carries each 0017 function body byte-identical to the migration (a test compares them, as `tests/data/test_migration_0016.py` does for 0016). Replace the 0016 bodies in `schema.sql`; do not keep both.
- Function signatures and return types stay as in 0016.
- New config families in `config.API_RESULT_LIMITS` (comment each one "Not a row cap: ..."): `literature_topic_scan` (3000000, 3000000), `literature_topic_chunk` (500, 500), `time_budget_ms` (20000, 20000), `search_work_mem_mb` (256, 256). Changed: `search_rank_chars` (2000, 2000). 0017 seeds the new families `ON CONFLICT DO NOTHING` and sets `search_rank_chars` to (2000, 2000) only where it still holds (10000, 10000). A test checks the seeds equal config.
- Gate marker for 0017 in `src/episteme/ops/gate_migrations.py`: `"table:episteme.mesh_qualifier"`.
- Deadline everywhere: `statement_timestamp() + make_interval(secs => limit_for('time_budget_ms') / 1000.0)`; "spent" means `clock_timestamp() >= deadline`.
- Note texts (exact):
  - `topic <btrim(p_topic)> is not a MeSH descriptor or qualifier name`
  - `partial: time budget of <n> ms spent; years from <y> down not searched`
  - `partial: time budget of <n> ms spent; records with an unknown year not searched`
  - `partial: time budget of <n> ms spent; not searched: <sources, comma+space separated>`
  - Unchanged 0016 note for a qualifier-only topic: `topic <t> is not a MeSH descriptor name in the indexed records; matched on qualifier names only`.
- No `os.environ` outside `config.py`; no endpoint URLs outside `scripts/data/_lib/sources.env`.
- Implementers have no database. Write `pg` tests; the controller runs them on `episteme_test`. Run `ruff format`, `ruff check` and `pytest -m "not pg"` on changed files before each commit. `git add` explicit paths only.

## Review Focus

1. A topic that is both a descriptor and a qualifier name (`mortality`): rows that match only by qualifier must still be returned (the descriptor side alone is not enough). Test in Task 2.
2. `mesh_qualifier` missing or empty: the 0016 results for every topic, unchanged (`test_unknown_topic_note` keeps passing without the table filled). Test in Task 2.
3. Budget spent in the middle of a window: rows already kept stay in the result, sorted and cut as usual, plus exactly one `partial:` note; no duplicate PMIDs. Test in Task 2.
4. A descriptor set larger than `literature_topic_scan`: the per-window probe gives the same rows as the exact set (set `literature_topic_scan` to 1 in a test and compare). Test in Task 2.
5. `search_text` leaves `work_mem` as it was after the call (the session value before = after). Test in Task 3.

---

### Task 1: `mesh_qualifier` data, config families, 0017 skeleton

**Files:**
- Create: `src/episteme/data/db/migrations/0017_api_no_timeouts.sql`
- Modify: `src/episteme/config.py` (`API_RESULT_LIMITS`), `src/episteme/data/graph_builder.py` (mesh phase), `src/episteme/data/db/grants/0015_api_tables.sql` (`c_read` gains `'mesh_qualifier'`), `src/episteme/ops/gate_migrations.py`
- Test: `tests/data/test_migration_0017.py` (new), `tests/reference/test_api_settings.py`, the existing graph_builder mesh test module (find it with `grep -rln "_parse_mesh_descriptors\|mesh_hierarchy" tests`), `tests/ops/test_gate_cli.py` if it lists markers.

**Interfaces:**
- Produces: table `episteme.mesh_qualifier (qualifier_ui text NOT NULL, qualifier_name text NOT NULL, source_file text)` with index `mesh_qualifier_lower_name_idx ON (lower(qualifier_name))`; config families above; `graph_builder._parse_mesh_qualifiers(xml_path: Path) -> dict[str, str]` ({QualifierUI: QualifierName}); `build(..., source="mesh")` result dict gains key `"mesh_qualifier"` (rows written).

- [ ] **Step 1: Failing tests**
  - `test_api_limits_defaults`: the dict gains the four families and `search_rank_chars: (2000, 2000)`.
  - `test_migration_0017.py` (non-pg): seeds in 0017 equal `config.API_RESULT_LIMITS` for the four new families; 0017 contains `UPDATE episteme_api_internal.result_limit` setting `search_rank_chars` to 2000/2000 `WHERE ... default_limit = 10000 AND max_limit = 10000`; `"0017_api_no_timeouts.sql"` maps to `["table:episteme.mesh_qualifier"]` in the gate.
  - `test_migration_0017.py` (pg): applying 0015, 0016, 0017, 0017 again succeeds; `to_regclass('episteme.mesh_qualifier')` not null.
  - Parse test (non-pg): a small descriptor XML with two `DescriptorRecord`s whose `AllowableQualifiersList` holds `<AllowableQualifier><QualifierReferredTo><QualifierUI>Q000009</QualifierUI><QualifierName><String>adverse effects</String></QualifierName></QualifierReferredTo><Abbreviation>AE</Abbreviation></AllowableQualifier>` (one shared, one distinct qualifier) gives `{"Q000009": "adverse effects", "Q000401": "mortality"}`; a gzip copy gives the same; malformed XML gives `{}`.
  - Mesh build test (pg, same module as the existing mesh_hierarchy build test): after `build(source="mesh")`, `mesh_qualifier` holds the qualifiers of the fixture file with `source_file` set; a second build does not duplicate rows; result `["mesh_qualifier"]` equals the row count.
- [ ] **Step 2: Run them; they fail.** `pytest tests/reference/test_api_settings.py tests/data/test_migration_0017.py -m "not pg" -v`
- [ ] **Step 3: Implement.**
  - Config families (Global Constraints).
  - 0017 skeleton: header comment (purpose, spec path, "apply after 0016 as episteme_sys_admin; run `api_settings sync` after it; then `run_pipeline.sh mesh graph` fills mesh_qualifier"), `BEGIN;`, seeds, the `search_rank_chars` update, `CREATE TABLE IF NOT EXISTS episteme.mesh_qualifier`, the index, `COMMIT;`. Tasks 2 and 3 add function bodies before `COMMIT;`.
  - `_parse_mesh_qualifiers`: same streaming, gzip and failure discipline as `_parse_mesh_descriptors` (read the `QualifierUI` and the `QualifierName/String` inside each `QualifierReferredTo`). In the mesh phase of `build`, per `src_file`, after `mesh_tree`: `DELETE FROM episteme.mesh_qualifier WHERE source_file = %s`, then COPY `(qualifier_ui, qualifier_name, source_file)`; add the count to the audit `rows_affected` and to the new result key. If `mesh_qualifier` does not exist (`to_regclass` null), skip it and print a WARNING to stderr naming migration 0017 (the mesh phase must still work before 0017).
  - Grants `c_read` += `'mesh_qualifier'`; gate marker.
- [ ] **Step 4: Run the tests; they pass** (non-pg locally; list the pg tests for the controller).
- [ ] **Step 5: Commit** `feat(api): 0017 skeleton — mesh_qualifier from the MeSH descriptor file, new limit families`

### Task 2: `literature_by_mesh` 0017 body

**Files:**
- Modify: `src/episteme/data/db/migrations/0017_api_no_timeouts.sql`, `src/episteme/data/db/schema.sql`, `src/episteme/reference/literature.py` (docstring only), `tests/data/test_migration_0017.py` (schema.sql body check)
- Test: `tests/reference/test_api_literature.py`

**Interfaces:**
- Consumes: `episteme.mesh_qualifier`, families `literature_topic_scan`, `literature_topic_chunk`, `time_budget_ms` (Task 1).
- Produces: new helper `episteme_api_internal.literature_topic_keep(p_pmid text[], p_year int[], p_ok text[], p_topic text, p_chunk int, p_deadline timestamptz) RETURNS TABLE (pmid text, year int, stopped boolean)` — the qualifier side: of the candidate rows (`p_pmid[i]`, `p_year[i]`, year NULL = unknown), keeps those in `p_ok` (already matched on the descriptor side) and those with an `article_mesh` row whose `qualifiers` hold a name with `lower(q) = p_topic`. Candidates not in `p_ok` are checked in chunks of `p_chunk`, ordered by year DESC NULLS LAST, pmid; the deadline is checked before each chunk. When spent: returns the rows kept so far, then one row (NULL, newest unchecked year, true) and stops. Otherwise every kept row has `stopped = false`. `LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp`, owned and granted like the other internal helpers in 0015/0016 (copy their pattern).
- `literature_by_mesh` signature unchanged.

Algorithm of the new body (the spec, decisions 1 to 4; keep every 0016 rule not named here: substance side, year source, year bound, window order and stop rule, unknown-year rule, step 4 output):
1. `v_deadline` per Global Constraints. Topic: `v_ui` and `v_uis` as in 0016. `v_qual` = true when `mesh_qualifier` is missing or empty, else `EXISTS (... WHERE lower(qualifier_name) = v_topic)`. If `v_ui` is NULL and not `v_qual`: the "neither" note, `RETURN` (no article rows, no hit scan). If `v_ui` is NULL and `v_qual`: the 0016 qualifier-only note.
2. Hits as in 0016 (`v_pmids`).
3. Descriptor side, when `v_ui` is not NULL: one statement reading `SELECT m.pmid FROM episteme.article_mesh m WHERE m.descriptor_ui = ANY (v_uis) LIMIT v_topic_scan + 1` as a fenced subquery, hash-joined with `unnest(v_pmids)`, giving the count of rows read and the sorted distinct joined PMIDs. Count ≤ `v_topic_scan`: `v_set` = those PMIDs (exact). Otherwise `v_set` = NULL (not exact). When exact and not `v_qual`: `v_pmids := v_set` and no window runs a topic check.
4. Windows as in 0016, except the topic check of a window's rows: no topic, or (exact and not `v_qual`): all rows kept. Otherwise `v_ok` = window PMIDs in `v_set` (exact) or the result of `SELECT DISTINCT t.pmid FROM episteme.article_mesh t WHERE t.descriptor_ui = ANY (v_uis) AND t.pmid = ANY (<window pmids>)` (not exact; empty when `v_ui` is NULL). Not `v_qual`: kept = `v_ok`. `v_qual`: kept = `literature_topic_keep(window rows, v_ok, ...)`; a `stopped` row ends the search with the "years from <y> down" note (`<y>` = its year).
5. Deadline at the top of each window after the first: spent → the "years from <w.hi> down" note, exit. The unknown-year step (0016 rule) runs only when no `partial` note was given; its topic check uses the same steps 3 and 4 with year NULL; spent before it or a `stopped` row inside it → the "unknown year" note.
6. Notes are returned before the article rows. Step 4 of 0016 (sort, cut, descriptors, source_file) unchanged.

- [ ] **Step 1: Failing tests** in `test_api_literature.py` (fixture: create `mesh_qualifier` like `mesh_tree` is created, empty by default; a helper fills it with `('Q000009','adverse effects')`, `('Q000401','mortality')`):
  - `test_neither_topic_returns_note_only`: qualifier table filled; `_call(db, 1, "No Such Thing")` → no ids, one note equal to the "neither" text.
  - `test_qualifier_only_topic_with_list`: list filled; `"adverse effects"` → `["pubmed:200"]` and the 0016 qualifier-only note.
  - `test_topic_both_descriptor_and_qualifier`: add descriptor rows so that `Mortality` (descriptor) is on one metformin PMID and qualifier `mortality` on another metformin row of a different PMID; list filled; `"mortality"` → both PMIDs.
  - `test_large_set_matches_exact_set`: for topics `"Liver Diseases"` and `"adverse effects"` and `"mortality"`: results with `literature_topic_scan` = 1 (update `result_limit` in the test) equal results with the default.
  - `test_budget_spent_gives_partial_note`: with `time_budget_ms` = 1 and the call made after a sleep in the same statement (for example `SELECT r.* FROM (SELECT pg_sleep(0.05)) s CROSS JOIN LATERAL episteme_api_internal.literature_by_mesh(1, CASE WHEN s.pg_sleep IS NULL THEN 'adverse effects' END) r` — the lateral reference makes the sleep run first): exactly one note starting `partial: time budget of 1 ms spent;`, no duplicate ids.
  - `test_topic_keep_stops_at_deadline` (helper direct): deadline in the past → one row `(NULL, <newest year>, true)` only; deadline in the future → kept rows, none `stopped`; `p_ok` rows kept without a qualifier.
  - Existing tests stay green unchanged (they run with the list empty: 0016 behaviour).
  - `test_migration_0017.py`: `schema.sql` carries the 0017 bodies of `literature_by_mesh` and `literature_topic_keep` byte-identical.
- [ ] **Step 2: Run** `pytest tests/data/test_migration_0017.py -m "not pg" -v` (fails); list the pg tests for the controller.
- [ ] **Step 3: Implement** the helper and the body in 0017 (header comment of the function updated to describe decisions 1 to 4, in the style of the 0016 comment) and copy both bodies into `schema.sql`; update the `literature.py` docstring (topic kinds, the partial note).
- [ ] **Step 4: Run the non-pg tests; they pass.**
- [ ] **Step 5: Commit** `perf(api): 0017 literature_by_mesh — topic set from the descriptor index, qualifier list, time budget`

### Task 3: `search_text` 0017 body

**Files:**
- Modify: `src/episteme/data/db/migrations/0017_api_no_timeouts.sql`, `src/episteme/data/db/schema.sql`, `tests/data/test_migration_0017.py`
- Test: `tests/reference/test_api_search.py`

**Interfaces:**
- Consumes: families `search_work_mem_mb`, `search_rank_chars`, `time_budget_ms` (Task 1).
- Produces: `search_text` body; signature unchanged.

Changes to the 0016 body (every other rule stays):
1. Start: `v_old_wm := current_setting('work_mem')`; `PERFORM set_config('work_mem', limit_for('search_work_mem_mb') || 'MB', true)`. Before the final `RETURN QUERY`: `PERFORM set_config('work_mem', v_old_wm, true)`. Also on every earlier `RETURN` taken after the change.
2. Rank input for every source: `fts_doc(b.source, b.title, b.abstract, left(b.body_text, v_rank_chars), left(b.text_override, v_rank_chars))` (the `CASE` on label sources goes away). The match expression is unchanged.
3. Deadline (Global Constraints) checked before each source of the candidate loop: spent → stop the loop; the not-searched sources (this one and the rest of `v_ready`) go into the "not searched" note, returned before the article rows; the rows found so far are ranked and returned as usual.
4. Header comment updated (rank input for all sources, work_mem, budget; contract change: scores move for long texts).

- [ ] **Step 1: Failing tests** in `test_api_search.py`:
  - `test_rank_uses_first_chars_for_every_source`: a pmc (or bookshelf) row whose query word appears only after character `search_rank_chars` still matches, with score 0; one where it is in the first characters scores > 0. Set `search_rank_chars` small (for example 50) in `result_limit` for the test.
  - `test_work_mem_restored`: `SHOW work_mem` before and after a `search_text` call in the same transaction are equal.
  - `test_budget_spent_names_unsearched_sources`: `time_budget_ms` = 1, sleep-first statement as in Task 2 → one note `partial: time budget of 1 ms spent; not searched: <all ready sources of the test, comma+space>` and no article rows.
  - `test_migration_0017.py`: `schema.sql` carries the 0017 `search_text` body byte-identical.
  - Update any existing test that expects the 0016 label-only rank input or the 10000 default.
- [ ] **Step 2: Run** non-pg tests; they fail.
- [ ] **Step 3: Implement** in 0017 and copy the body into `schema.sql`.
- [ ] **Step 4: Run the non-pg tests; they pass.**
- [ ] **Step 5: Commit** `perf(api): 0017 search_text — exact bitmap, short rank input for all sources, time budget`

### Task 4: Runbook, drift log, spec cross-references

**Files:**
- Modify: `docs/10-data-sources-runbook.md`, `docs/project-incubation-baseline.md`, `docs/superpowers/specs/2026-11-24-api-layer-perf-design.md` (one line under Status: "Second addendum: 2026-11-24-api-no-timeouts-design.md (0017)").

- [ ] **Step 1: Runbook.**
  - Migration table: a `0017_api_no_timeouts.sql` row (same columns as the 0016 row): what it adds, idempotent, one transaction, then `api_settings sync`, then `run_pipeline.sh mesh graph` to fill `mesh_qualifier`.
  - §5.19.5: after each index swap transaction, run `ANALYZE <table>` at once, with the reason (0016 rollout: no expression statistics after the swap → parallel sequential scan, 227 s).
  - A 0017 rollout subsection after the 0016 one: apply 0017 on `episteme_test`, then `episteme`; `api_settings sync` on both; `run_pipeline.sh mesh graph` on `episteme`; check `SELECT count(*) FROM episteme.mesh_qualifier` (expected 76 for desc2026); acceptance per spec decision 7 through the reader MCPg instance.
  - The config families table (if the runbook lists families): the four new ones and the new `search_rank_chars` default.
- [ ] **Step 2: Drift log** (`docs/project-incubation-baseline.md`): a 2026-11-24 entry "0017 no-timeouts": what changed (decisions 1 to 6 in one line each), the measured causes (spec table), the contract changes (neither-topic note, partial notes, rank input), and that the rollout results are added after the merge.
- [ ] **Step 3: Check** `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'` is empty; `ruff format --check` and `ruff check` pass.
- [ ] **Step 4: Commit** `docs(api): 0017 runbook rollout, ANALYZE after index swap, drift log`
