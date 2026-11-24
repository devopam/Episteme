# SQL lookup API performance follow-up — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `literature_by_mesh`, `graph_neighbours(..., 'shared_mesh')` and the label `search_text` finish well inside the reader's 30 s timeout on production, with the 0015 contracts kept (except the label rank input, decision 6).

**Architecture:** One new migration `0016_api_perf.sql` replaces three internal functions (`CREATE OR REPLACE`, same signatures) and is mirrored in `schema.sql`. Two config families are added to `API_RESULT_LIMITS`. Two `article_mesh` indexes change shape by a runbook step (covering, same names). Docs carry the rollout, the runbook corrections from the 0015 rollout and the drift-log entries.

**Tech Stack:** PostgreSQL 19beta3 (plpgsql), Python 3 / psycopg 3, pytest (`pg` marker), ruff.

**Spec:** `docs/superpowers/specs/2026-11-24-api-layer-perf-design.md` (addendum to `docs/superpowers/specs/2026-11-17-graphrag-api-layer-design.md`).

## Global Constraints

- Implementers have **no database**. Write the SQL and the tests; run `ruff format`, `ruff check` and `pytest -m "not pg"` only. The controller runs the `pg` tests on `episteme_test` and reports failures back.
- Every function keeps `SECURITY DEFINER`, `SET search_path = pg_catalog, pg_temp`, schema-qualified names, its 0015 signature and return type. `episteme_api.*` public wrappers are not changed.
- `0016_api_perf.sql` must apply after `0015_api_layer.sql`, twice in a row without error (idempotent), and on a database where the `episteme` tables are empty.
- `schema.sql` carries the same function bodies as the newest migration (as #119 did for 0015): replace the 0015 copies of the changed functions in `schema.sql` with the 0016 bodies.
- `tests/reference/_pbrer_db.py` `MIGRATIONS` list gets `0016_api_perf.sql` after `0015_api_layer.sql`.
- New config families go in `config.API_RESULT_LIMITS` with a comment like `graph_scan`/`search_scan` ("Not a row cap: ..."): `"literature_window": (5, 5)` and `"search_rank_chars": (10000, 10000)`. Functions read them with `episteme_api_internal.limit_for('<family>')`.
- Docs name env keys only, never values. Endpoint URLs only in `scripts/data/_lib/sources.env`.
- Commits: explicit `git add` paths, never `--no-verify`. Messages end with the attribution lines the controller gives.
- Writing style of docs: ASD-STE100 spirit (short direct sentences, one action per step).

## Review Focus

1. A PMID with two `pubmed` rows in different year windows (for example versions with years 2019 and 2021): it must appear once, with `max` year, in the higher window only.
2. `p_limit` smaller than the rows of the first window: ties at the cut are ordered by `article_id` and the cut is exact (the wrapper asks for limit + 1 and reports truncation).
3. A topic that matches no row in the first windows but matches older rows: windows keep going until rows are found or the years run out.
4. Year filter with `p_from_year > p_to_year`, or a filter outside every row: empty result, no error, no endless loop.
5. A label match where the query words appear only after `search_rank_chars` characters: the row is still returned (match unchanged), with a lower or zero score.

---

### Task 1: `literature_by_mesh` with year windows

**Files:**
- Create: `src/episteme/data/db/migrations/0016_api_perf.sql`
- Modify: `src/episteme/data/db/schema.sql` (the `literature_by_mesh` internal function body)
- Modify: `src/episteme/config.py` (`API_RESULT_LIMITS`: `literature_window`, `search_rank_chars`)
- Modify: `src/episteme/ops/gate_migrations.py` (marker for 0016; add a `function` marker kind if needed)
- Modify: `tests/reference/_pbrer_db.py` (`MIGRATIONS`)
- Test: `tests/reference/test_api_literature.py`, `tests/data/test_migration_0016.py`, the existing unit test of `gate_migrations` markers

**Interfaces:**
- Produces: `episteme_api_internal.literature_by_mesh(p_substance_key bigint, p_topic text, p_from_year int DEFAULT NULL, p_to_year int DEFAULT NULL, p_limit int DEFAULT NULL) RETURNS SETOF episteme_api_internal.article_row` (signature of 0015, new body).
- Produces: `episteme_api_internal.year_windows(p_from int, p_to int, p_first_width int, p_now int) RETURNS TABLE (lo int, hi int, n int)`: the descending windows (see Step 3). It is the gate marker of 0016: `function:episteme_api_internal.year_windows`.

- [ ] **Step 1: Write the failing tests** (pg; add to the existing fixture where possible)

```python
@pytest.mark.pg
def test_year_from_pubmed_row_only(db):
    # A manuscript row with a later year must not move the PubMed year.
    with db.cursor() as cur:
        cur.execute("INSERT INTO episteme.articles (id, source, pmid, year) VALUES "
                    "('europepmc_manuscript:M100', 'europepmc_manuscript', '100', 2025)")
    db.commit()
    rows = _call(db, 1, None)
    assert {r[1]: r[4] for r in rows if r[0] == "article"}["pubmed:100"] == 2020

@pytest.mark.pg
@pytest.mark.parametrize("width", [1, 2, 5, 50])
def test_window_width_does_not_change_results(db, width):
    expected = _call(db, 1, None)
    expected_f = _call(db, 1, None, 2020, 2022)
    expected_t = _call(db, 1, "Liver Diseases")
    expected_1 = _call(db, 1, None, limit=1)
    with db.cursor() as cur:
        cur.execute("UPDATE episteme_api_internal.result_limit SET default_limit = %s, "
                    "max_limit = greatest(max_limit, %s) WHERE family = 'literature_window'",
                    (width, width))
    db.commit()
    assert _call(db, 1, None) == expected
    assert _call(db, 1, None, 2020, 2022) == expected_f
    assert _call(db, 1, "Liver Diseases") == expected_t
    assert _call(db, 1, None, limit=1) == expected_1

@pytest.mark.pg
def test_pmid_in_two_windows_appears_once(db):
    with db.cursor() as cur:
        cur.execute("INSERT INTO episteme.articles (id, source, pmid, year) VALUES "
                    "('pubmed:600v2', 'pubmed', '600', 2012)")
    db.commit()
    ids = _ids(_call(db, 1, None))
    assert ids.count("pubmed:600") == 1

@pytest.mark.pg
def test_reversed_or_empty_year_range(db):
    assert _ids(_call(db, 1, None, 2024, 2020)) == []
    assert _ids(_call(db, 1, None, 1, 5)) == []
```

Also, in `tests/data/test_migration_0016.py` (pg): apply 0015 then 0016 twice on `episteme_test`; `year_windows` exists; `SELECT * FROM episteme_api_internal.year_windows(NULL, NULL, 5, 2026)` returns first row `(2023, 9999, 1)`, second `(2013, 2022, 2)`, each next window twice the width of the one before, the last `lo` = 1; `year_windows(2020, 2024, 5, 2026)` returns exactly one row `(2020, 2024, 1)`; `year_windows(2024, 2020, 5, 2026)` returns no row.

Every existing test in `tests/reference/test_api_literature.py` must still pass unchanged.

- [ ] **Step 2: Run the non-pg suite**

Run: `pytest -m "not pg" -q` → PASS. (The pg tests run by the controller fail before Step 3.)

- [ ] **Step 3: Write `0016_api_perf.sql` (literature part) and the config families**

`year_windows(p_from, p_to, p_first_width, p_now)`: `LANGUAGE sql IMMUTABLE`. Bounds `lo_min := coalesce(p_from, 1)`, `hi_max := coalesce(p_to, 9999)`. No row when `lo_min > hi_max`. First window: `hi = hi_max`, `lo = greatest(lo_min, least(hi_max, p_now + 1) - p_first_width + 1)`. Each next window ends at the previous `lo - 1`, is twice as wide as the previous width, and is clamped at `lo_min`. Column `n` numbers the windows from 1. (`p_now` is a parameter so the function is IMMUTABLE and testable; the caller passes `extract(year from current_date)::int`.)

`literature_by_mesh` body, in this order (keep the 0015 note/missing rows, the `v_names` and topic-descriptor expansion `v_uis` exactly as they are):

1. Hits: `v_pmids := ARRAY(SELECT DISTINCT m.pmid FROM episteme.article_mesh m WHERE lower(m.descriptor_name) = ANY (v_names))`. Read only `pmid` (covering index, decision 1). No topic check here.
2. For each row of `year_windows(p_from_year, p_to_year, limit_for('literature_window'), extract(year from current_date)::int)` in order: add to `v_found` (pmid, year) the rows
   `SELECT a.pmid, max(a.year) FROM episteme.articles a WHERE a.source = 'pubmed' AND a.year BETWEEN w.lo AND w.hi AND a.year > 0 AND a.pmid = ANY (v_pmids) AND NOT (a.pmid = ANY (<pmids already in v_found>)) GROUP BY a.pmid`, then keep only those that pass the topic check (the 0015 `EXISTS` on `article_mesh t WHERE t.pmid = ...`, unchanged) when `v_topic <> ''`. Stop after a window when `cardinality(v_found) >= p_limit` (NULL `p_limit`: never stop early).
   A PMID whose `pubmed` rows lie in two windows is taken by the higher window, with `max(year)` of the rows in that window; it must not be counted again (Review Focus 1). Use the `pmid` partial index: write the predicate so `pmid IS NOT NULL` is implied (`a.pmid = ANY(...)` does that).
3. Unknown years: only when `p_from_year IS NULL AND p_to_year IS NULL` and the windows ran out below `p_limit`: the hits not in `v_found` (no `pubmed` row, or year 0) that pass the topic check, with year NULL.
4. Output: order `year DESC NULLS LAST, 'pubmed:' || pmid` (as 0015), `LIMIT p_limit`; then, for those rows only, `descriptors` = `array_agg(DISTINCT m.descriptor_name ORDER BY m.descriptor_name)` and `source_file` = `min(m.source_file)` from `article_mesh m WHERE m.pmid = <row pmid> AND lower(m.descriptor_name) = ANY (v_names)`. All other output columns as 0015.

Header comment of the function: the contract (unchanged), the windows, the PubMed-only year (spec decision 3) and the index it relies on.

Config: add both families with comments (Global Constraints). Gate: marker `function:episteme_api_internal.year_windows` for `0016_api_perf.sql`; if `gate_migrations` has no `function` kind, add it (`to_regprocedure`/`pg_proc` lookup by qualified name) with a unit test like the other kinds.

- [ ] **Step 4: Mirror in `schema.sql`, add 0016 to `_pbrer_db.MIGRATIONS`, run `ruff format`, `ruff check`, `pytest -m "not pg"`** → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/episteme/data/db/migrations/0016_api_perf.sql src/episteme/data/db/schema.sql src/episteme/config.py src/episteme/ops/gate_migrations.py tests/reference/_pbrer_db.py tests/reference/test_api_literature.py tests/data/test_migration_0016.py <gate test file>
git commit -m "perf(api): literature_by_mesh by year windows; PubMed year only (0016)"
```

### Task 2: `graph_neighbours` id lookup and `shared_mesh`

**Files:**
- Modify: `src/episteme/data/db/migrations/0016_api_perf.sql` (append `graph_neighbours` internal function)
- Modify: `src/episteme/data/db/schema.sql`
- Test: `tests/reference/test_api_graph.py`

**Interfaces:**
- Consumes: 0016 from Task 1.
- Produces: `episteme_api_internal.graph_neighbours(p_article_id text, p_kind text, p_limit int DEFAULT NULL)` (0015 signature, new body).

- [ ] **Step 1: Write the failing tests** (pg)

```python
@pytest.mark.pg
def test_shared_mesh_year_from_pubmed_row_only(<fixture>):
    # a europepmc_manuscript row with a later year for a neighbour PMID: the row's year
    # is the pubmed year
    ...
@pytest.mark.pg
def test_id_lookup_takes_lowest_pmid(<fixture>):
    # two articles rows with id 'pmcid:PMCX' and pmids '20' and '100': kind 'cites'
    # resolves to '100' (COLLATE "C" order: '100' < '20'), as 0015
    ...
```

Every existing test in `tests/reference/test_api_graph.py` must still pass unchanged (same rows, order, scores, `source_file`).

- [ ] **Step 2: Run `pytest -m "not pg" -q`** → PASS.

- [ ] **Step 3: Change the body**

- Id lookup (non-numeric id): `SELECT min(a.pmid COLLATE "C") INTO v_pmid FROM episteme.articles a WHERE a.id = v_id AND a.pmid <> ''` — no `ORDER BY`, no `LIMIT` (spec decision 5). Same result as 0015.
- `shared_mesh`: the per-descriptor count and the `hit` step read only `descriptor_ui` and `pmid` of `article_mesh` (index-only, decision 1): drop `min(m.source_file)` from `hit` and `sc`. After `top`, compute per returned row `sf = min(m.source_file) FROM episteme.article_mesh m WHERE m.pmid = t.pmid AND m.descriptor_ui = ANY (v_uis)` (same value as 0015) and `year = NULLIF(max(a.year), 0) FROM episteme.articles a WHERE a.source = 'pubmed' AND a.pmid = t.pmid` (decision 3).
- `cites`, `cited_by`, `trial`, `label` kinds: unchanged.

- [ ] **Step 4: Mirror in `schema.sql`; `ruff format`, `ruff check`, `pytest -m "not pg"`** → PASS.

- [ ] **Step 5: Commit** — `perf(api): graph_neighbours id lookup and index-only shared_mesh (0016)`

### Task 3: `search_text` rank input for labels

**Files:**
- Modify: `src/episteme/data/db/migrations/0016_api_perf.sql` (append `search_text` internal function)
- Modify: `src/episteme/data/db/schema.sql`
- Test: `tests/reference/test_api_search.py`

**Interfaces:**
- Produces: `episteme_api_internal.search_text(p_query text, p_sources text[] DEFAULT NULL, p_limit int DEFAULT NULL)` (0015 signature, new body).

- [ ] **Step 1: Write the failing tests** (pg)

```python
@pytest.mark.pg
def test_label_rank_uses_leading_text_match_uses_all(<fixture>):
    # Set search_rank_chars to 100 (result_limit row). A dailymed body whose query word
    # appears only after character 100 is still returned (match unchanged) with score 0.0;
    # a dailymed body with the word in the first 100 characters scores > 0 and comes first.
    ...
@pytest.mark.pg
def test_non_label_rank_unchanged(<fixture>):
    # pubmed rows: same scores as ts_rank_cd(fts_doc(<full>), q) — compare with a direct query.
    ...
```

The existing test that checks the match uses `fts_index.fts_expression` exactly must still pass unchanged.

- [ ] **Step 2: Run `pytest -m "not pg" -q`** → PASS.

- [ ] **Step 3: Change the body**

Only the rank expression changes. For `v_src IN ('dailymed', 'ema_epar')` rank with
`ts_rank_cd(episteme_api_internal.fts_doc(b.source, b.title, b.abstract, left(b.body_text, v_rank_chars), left(b.text_override, v_rank_chars)), v_q)`, `v_rank_chars := limit_for('search_rank_chars')`; for every other source the 0015 expression. The `WHERE ... @@ v_q` predicate stays the exact index expression. Update the function's header comment (rank input for label sources; spec decision 6).

- [ ] **Step 4: Mirror in `schema.sql`; `ruff format`, `ruff check`, `pytest -m "not pg"`** → PASS.

- [ ] **Step 5: Commit** — `perf(api): rank label search on the leading label text (0016)`

### Task 4: Runbook, drift log, reader MCPg entry

**Files:**
- Modify: `docs/10-data-sources-runbook.md`
- Modify: `docs/project-incubation-baseline.md` (drift log)
- Modify: `docs/superpowers/specs/2026-11-17-graphrag-api-layer-design.md` (one pointer line to the addendum)
- Modify: `.mcp.json` (add `mcpg-reader`)
- Test: `tests/docs/` (any doc test that lists migrations or runbook sections; run `pytest -m "not pg" tests/docs`)

- [ ] **Step 1: Runbook.**
  - Migrations table: row for `0016_api_perf.sql` (replaces three API functions; no table change; apply to `episteme_test`, run `api_settings sync`, then `episteme`; light).
  - New subsection in 5.19 "Covering indexes on `article_mesh` (once)": as `episteme_sys_admin`, with no graph load running, one at a time: `CREATE INDEX article_mesh_lower_name_cov_idx ON episteme.article_mesh (lower(descriptor_name)) INCLUDE (descriptor_name, pmid);` then `DROP INDEX episteme.article_mesh_lower_name_idx;` then `ALTER INDEX episteme.article_mesh_lower_name_cov_idx RENAME TO article_mesh_lower_name_idx;` (the partition indexes keep generated names; only the parent name matters to `bulk_indexes`). The same for `article_mesh_descriptor_ui_idx` with `(descriptor_ui, pmid)`. Say: each build reads ~345 million rows and blocks writes to `article_mesh`, not reads; expect tens of minutes and tens of GB on E:. Update the existing `article_mesh_lower_name_idx` paragraph to the covering definition.
  - Upkeep: run `VACUUM (ANALYZE)` on `article_mesh` after `graph --bulk` (index-only scans need the visibility map); `ANALYZE` large tables after a bulk load (stale statistics made `articles_default` pick a full index walk, 2026-11-24).
  - Corrections from the 0015 rollout (section 5.10 / 5.19 rollout steps): expected EU label links **20** (not 21); expected `not_downloaded` about **4,600** (not about 20,300); load markers are shared by `episteme_test` and `episteme` (one processed dir), so the production load after a test load needs `--force --reason "..."`; the `ema` feeds must be loaded in the target database before `ema_epar load` (it needs the latest `ema_medicines` release in `reg_release`).
  - Changelog row v8, 2026-11-24.
- [ ] **Step 2: Drift log** (`docs/project-incubation-baseline.md`, newest entry at the place the file's convention says): 2026-11-24 entry: 0015 rollout steps 1–11 done on both databases (FAERS forced load 3 h 18 min; FTS index builds: pubmed 6,151 s, openalex 11,600 s, dailymed 359 s, pmc 234 s); step 11 results (every function works through the reader; restricted mode refuses `SET` and `set_config`; base tables and internal functions refused); the three slow lookups and their causes (spec table); `ANALYZE episteme.articles_default` run on production 2026-11-24; 98 never-analyzed large tables (runbook step, not run); FAERS: 4 reports with impossible quarter years (0090, 1010, 1908, 1919), source data, not fixed; owner items: `effective_io_concurrency = 2`, `io_workers` default, drive random-read speed.
- [ ] **Step 3: `.mcp.json`**: add after `mcpg-test`:
```json
    "mcpg-reader": {
      "command": "bash",
      "args": ["scripts/mcpg-episteme.sh", "episteme", "--reader"],
      "env": {}
    }
```
- [ ] **Step 4: Run `pytest -m "not pg" -q`** → PASS.
- [ ] **Step 5: Commit** — `docs(api): 0016 rollout, article_mesh covering indexes, 0015 rollout corrections`

---

## Rollout and acceptance (controller, after the merge; one heavy job at a time)

1. Apply 0016 to `episteme_test`; `api_settings sync`; run the reference `pg` tests.
2. Apply 0016 to `episteme`; `api_settings sync` (needs the two new families).
3. Build the two covering indexes on `episteme` (runbook steps), one after the other.
4. Acceptance (spec decision 7): through the reader MCPg (`scripts/mcpg-episteme.sh episteme --reader`), fresh connection: `literature_by_mesh` for acetaminophen 19464, ibuprofen 149266, simvastatin 69503 without a filter; one year-filtered call; one topic call; one rare-topic call on a large substance (aspirin 127871); `graph_neighbours('pmcid:<id>', 'shared_mesh')`; `search_text` with `p_sources` `'{dailymed,ema_epar}'`. Pass: every call under 10 s. Report every time; a miss is reported, not hidden. Then `VACUUM (ANALYZE) episteme.article_mesh` and `EXPLAIN (ANALYZE, BUFFERS)` of one window probe and the `shared_mesh` hit step: Index Only Scan, Heap Fetches near 0.
