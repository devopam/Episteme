# GraphRAG API Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the reference lookups into SQL functions (`episteme_api`) that a read-only MCPg instance can call. Add full-text search, a FAERS seriousness/outcome table, and the `ema_epar` source built from offline data.

**Architecture:**
- Migration 0015 creates two schemas:
  - `episteme_api_internal` holds the settings tables, the shared helpers and the complete ("internal") lookup functions.
  - `episteme_api` holds the capped public functions.
- Functions are `STABLE SECURITY DEFINER`, owned by `episteme_api_owner`.
- The Python lookups in `src/episteme/reference/` keep their signatures and call the internal functions.
- Roles and grants live in a superuser grants file.
- EPAR and the FTS indexes are separate operator steps.

**Tech Stack:** PostgreSQL 19beta3 (SQL and PL/pgSQL), Python 3.13, psycopg 3, pytest (the `pg` marker is for database tests), ruff.

**Spec:** `docs/superpowers/specs/2026-11-17-graphrag-api-layer-design.md` (owner-approved 2026-11-17).

## Global Constraints

- Server `19beta3`. Database tests are marked `pg`. Implementers write them but **do not run them**: they have no database. The controller runs them on `episteme_test` with `.env` sourced.
- `src/episteme/config.py` is the only module that reads `os.environ`. Never read, print or copy `.env`. Docs name env keys only.
- Endpoint URLs live only in `scripts/data/_lib/sources.env`. The URL guard must print nothing: `grep -REn 'ftp\.|s3://|https?://' scripts/data/ | grep -v _lib/sources.env | grep -vE ':[0-9]+:\s*#'`.
- Every extractor or serializer keys its checkpoint and its `source_file` on `input_key(path, raw_dir)`, with a collision test.
- No MedDRA, WHODrug or CDISC coding or hierarchy logic. FAERS reaction terms are quoted as published.
- No EMA (or other) download, request or retry.
- Functions only read. The reader role gets `USAGE` on `episteme_api` and `EXECUTE` on its functions, and nothing else in `episteme`, `episteme_api_internal` or the AACT schemas.
- Every function result row carries `source`, `release_id`, `licence` and `source_ref`/`record_key`.
- `licence` uses the `LOOKUP_SOURCE_SUBSETS` vocabulary (`commercial`, `pending`, `open_metadata`). `pending` rows are returned, not hidden.
- Tunables go in `config.py` and `.env.example` (commented defaults), never as source constants.
- Git: `git add` explicit paths; never `--no-verify`; `ruff format` and `ruff check` before every commit. Each commit message ends with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z`
- Non-pg test command: `.venv/Scripts/python.exe -m pytest -q -m "not pg" <paths>`.

## Shared shapes (used by every function task)

- **`episteme_api_internal.claim_row`** (composite type, created by Task 1). One row is one claim, one coverage note or one missing source:
  - `kind text`: `claim`, `note` or `missing`;
  - `claim_group int`, `alt_rank int` (0 = the claim, >0 = an alternative of that group);
  - `text text`, `value text`, `method text`, `confidence real`, `confidence_basis text`;
  - **As built (R5):** `confidence` is `double precision`, so `1/n` is exact in Python.
  - `source text`, `release_id text`, `source_file text`, `record_key text`, `jurisdiction text`;
  - `licence text`.
- **`episteme_api_internal.article_row`** (composite type, Task 1):
  - `kind text`: `article` or `note`;
  - `article_id text`, `source text`, `title text`, `year int`, `score real`, `relation text`;
  - `release_id text`, `licence text`, `note text`.
  - **As built (R9):** `article_row` ends with `source_file text` (11 columns).
- **Python mapping** (`episteme.reference._api.rows_to_result(rows) -> LookupResult`, Task 2):
  - `claim` rows become `Claim`/`Provenance`, with alternatives attached by `claim_group`/`alt_rank`;
  - `note` rows become `Coverage.notes`, and `missing` rows become `Coverage.missing`;
  - `matched` counts the `claim` rows with `alt_rank = 0`.
- **Public wrapper rule.** Every `episteme_api.f(...)` calls `episteme_api_internal.f(...)` and keeps the first `episteme_api_internal.limit_for('<family>')` claim/article rows in the internal order. All `note`/`missing` rows are kept. When rows are cut, it appends one `note` row: `truncated: <kept> of <total>`. Families: `regulatory`, `label`, `trials`, `faers`, `literature`, `graph`, `search`, `code`, `resolve`.

## Review Focus

1. **Unknown or ambiguous substance input.** `resolve_entity` with no match returns zero claims and one `missing` row. It must not raise. Ambiguous input returns alternatives, not a guess. Tests are in Task 2.
2. **Empty or missing source.** A dataset with no `is_latest` release, or a missing relation, gives a `missing` row while the other sources still answer (today's behaviour). Tests are in Tasks 3–6.
3. **Hostile search text.** `search_text` gets quotes, `&|!:*`, a 10,000-character string, an empty string or only stop words. It returns a bounded result or one `note` row, never an error. Test in Task 9.
4. **A common substance with a huge result** (for example paracetamol labels). The public function caps and adds the truncation note, and the internal function returns everything. Test in Task 4.
5. **The reader role tries to escape.** `SELECT` on a base table, calling `episteme_api_internal.*`, calling a function with `SET search_path` games, or calling a function inside a read-only transaction: all must be refused or harmless. Tests are in Task 10.

---

### Task 1: Migration 0015 skeleton, settings tables and the settings sync

**Files:**
- Create: `src/episteme/data/db/migrations/0015_api_layer.sql`
- Create: `src/episteme/reference/api_settings.py`
- Modify: `src/episteme/config.py` (new tunables), `.env.example`, `src/episteme/data/db/schema.sql` (final shape)
- Test: `tests/data/test_migration_0015.py` (pg), `tests/reference/test_api_settings.py` (non-pg), `tests/test_config_tunables.py`

**Interfaces:**
- Produces:
  - schemas `episteme_api` and `episteme_api_internal`;
  - types `episteme_api_internal.claim_row` and `article_row` (shapes above);
  - tables `episteme_api_internal.source_subset(source text primary key, subset text not null check (subset in ('commercial','pending','open_metadata')))` and `episteme_api_internal.result_limit(family text primary key, default_limit int not null, max_limit int not null, check (0 < default_limit and default_limit <= max_limit))`;
  - functions `episteme_api_internal.norm_name(p text) returns text` (`lower(normalize(p, NFKC))`, `IMMUTABLE`, NULL-safe), `episteme_api_internal.licence_for(p_source text) returns text` (the subset, else `'unlisted'`) and `episteme_api_internal.limit_for(p_family text) returns int`;
  - `config.API_RESULT_LIMITS: dict[str, tuple[int, int]]` with these defaults (default, max): regulatory (200, 1000), label (200, 2000), trials (200, 1000), faers (500, 5000), literature (50, 500), graph (50, 500), search (20, 200), code (1, 1), resolve (10, 50). Env overrides go through `EPISTEME_API_LIMIT_<FAMILY>`, with the value `<default>,<max>`.
  - `api_settings.sync(conn) -> dict[str, int]`, which writes both tables from `LOOKUP_SOURCE_SUBSETS` and `API_RESULT_LIMITS` in one transaction (delete plus insert) and returns the row counts;
  - the command `python -m episteme.reference.api_settings sync`.

- [ ] **Step 1: Write the tests.**
  - pg tests:
    - `test_0015_is_idempotent`: apply twice.
    - `test_norm_name_matches_nfkc_lower`: `'Ｃlopidogrel'` gives `'clopidogrel'`, `NULL` gives `NULL`.
    - `test_licence_for_unlisted`.
    - `test_limit_for_reads_table`.
    - `test_sync_writes_config`: after `sync`, `source_subset` equals `LOOKUP_SOURCE_SUBSETS` and `result_limit` equals `API_RESULT_LIMITS`.
  - Non-pg tests:
    - `test_api_limits_defaults`.
    - `test_api_limit_env_override`: `EPISTEME_API_LIMIT_SEARCH=5,50` gives `(5, 50)`.
    - `test_api_limit_env_rejects_bad`: `0,5`, `10,5` and `x` raise `ValueError`.
- [ ] **Step 2: Run the non-pg tests.** Expect FAIL (import error).
- [ ] **Step 3: Implement.**
  - The migration has its own `BEGIN;`/`COMMIT;` and uses `CREATE ... IF NOT EXISTS` / `CREATE OR REPLACE`.
  - No roles or grants go here (Task 10).
  - Mirror the final shape in `schema.sql`.
- [ ] **Step 4: Run the non-pg tests, `ruff format`, `ruff check`.** Expect PASS. Confirm that `pytest --collect-only tests/data/test_migration_0015.py` collects.
- [ ] **Step 5: Commit** `feat(api): migration 0015 skeleton, settings tables, settings sync`.

### Task 2: `resolve_entity`, `substance_family` and the Python mapping

**Files:**
- Modify: `0015_api_layer.sql` (append the functions), `src/episteme/reference/resolve.py`, `src/episteme/reference/_scope.py`
- Create: `src/episteme/reference/_api.py`; `tests/reference/_legacy.py` (a verbatim copy of today's query paths of resolve/_scope/lookups/literature/codes, used only for parity)
- Test: `tests/reference/test_api_resolve.py` (pg), `tests/reference/test_api_mapping.py` (non-pg)

**Interfaces:**
- Consumes: Task 1 types and helpers.
- Produces:
  - `episteme_api_internal.resolve_entity(p_text text, p_id_type text default null) returns setof claim_row`;
  - `episteme_api_internal.substance_family(p_substance_key bigint) returns table(substance_key bigint, moiety_key bigint, unii text, preferred_name text)`;
  - public wrappers `episteme_api.resolve_entity(...)` and `episteme_api.substance_family(...)`;
  - `_api.rows_to_result(rows: list[tuple]) -> LookupResult` and `_api.call(conn, function: str, *args) -> list[tuple]` (`function` must be a name in `_api.INTERNAL_FUNCTIONS`; arguments are bound as parameters, and the function name goes through `sql.Identifier`).
- Behaviour that differs from today (spec section 4.3, parked minors):
  - the UNII arm is a `UNION` branch and respects `p_id_type`;
  - `release_id` is filled from the substance source's `is_latest` release;
  - names go through `norm_name`.

- [ ] **Step 1: Write the tests.**
  - pg tests:
    - `test_resolve_parity`: legacy vs new on fixtures. Cases: exact UNII, name, salt name, unknown name, ambiguous name, `id_type='unii'` given a name.
    - `test_resolve_unknown_is_missing_row`: no exception.
    - `test_resolve_release_filled`.
    - `test_family_matches_scope`.
    - `test_public_resolve_caps`: 60 fixture alternatives give 50 rows plus the `truncated: 50 of 60` note.
  - Non-pg tests: `test_rows_to_result_alternatives`, `test_rows_to_result_notes_missing`, `test_call_rejects_unknown_function`.
- [ ] **Step 2: Run the non-pg tests.** Expect FAIL.
- [ ] **Step 3: Implement.** `resolve.resolve_entity` keeps its signature and calls `_api.call`. `_scope.substance_scope` reads `substance_family`.
- [ ] **Step 4: Run the non-pg tests and ruff; check that the pg tests collect.** Expect PASS.
- [ ] **Step 5: Commit** `feat(api): resolve_entity and substance_family in SQL`.

### Task 3: `approval_status` and `safety_actions`

**Files:** Modify `0015_api_layer.sql` and `src/episteme/reference/lookups.py`. Test: `tests/reference/test_api_regulatory.py` (pg).

**Interfaces:**
- Consumes: `substance_family`, `licence_for`, `claim_row`, `_api.call`/`rows_to_result`.
- Produces:
  - `episteme_api_internal.approval_status(p_substance_key bigint) returns setof claim_row`;
  - `episteme_api_internal.safety_actions(p_substance_key bigint, p_from date, p_to date) returns setof claim_row`;
  - the public wrappers (family `regulatory`).
- These are the same datasets, payload paths, claim texts and coverage notes as `lookups.py` today (`PBRER_DATASETS`, `_get`, `_reg_claim`), including `<dataset>: status field not found`. SQL reads the `jsonb` payload paths that `_get` reads.

- [ ] **Step 1: Write the tests.**
  - `test_approval_parity`: legacy vs new.
  - `test_safety_parity_interval`: an interval boundary on `event_date`.
  - `test_missing_dataset_row`: drop the latest flag of one dataset in the fixture; the others still answer.
  - `test_pending_rows_returned_with_tag`.
- [ ] **Step 2: Check that the tests collect.**
- [ ] **Step 3: Implement.** The Python functions keep their signatures and call `_api`.
- [ ] **Step 4: Run ruff and the non-pg suite of `tests/reference`.** Expect PASS.
- [ ] **Step 5: Commit** `feat(api): approval_status and safety_actions in SQL`.

### Task 4: `current_label`, `label_section_text` and `trials`

**Files:** Modify `0015_api_layer.sql` and `lookups.py`. Test: `tests/reference/test_api_label_trials.py` (pg).

**Interfaces:**
- Produces:
  - `episteme_api_internal.current_label(p_substance_key bigint) returns setof claim_row` (DailyMed and `ema_epar` labels through `label_substance`);
  - `episteme_api_internal.label_section_text(p_article_id text, p_section text) returns setof claim_row`. `p_section` matches the LOINC code or the case-insensitive title. The value is `substr(text, start_offset + 1, end_offset - start_offset)` from `article_body.text`;
    - **As built (R6):** the slice is from `article_body.body_text`, not the virtual `text`: the extractors write the offsets against `body_text`.
  - `episteme_api_internal.trials(p_substance_key bigint) returns setof claim_row`. It reads the AACT schema of the latest `aact` release, found through `reg_release`; `format('%I', ...)` builds the schema name in PL/pgSQL. It fills `source_file`;
  - public wrappers (families `label` and `trials`).

- [ ] **Step 1: Write the tests.**
  - `test_label_parity`.
  - `test_label_section_text_offsets`: a fixture text with two sections; the slices are exact.
  - `test_label_section_unknown_section_note`.
  - `test_trials_parity_and_source_file`.
  - `test_public_label_truncates`: 250 fixture sections give 200 plus the note, and the internal function gives 250.
- [ ] **Step 2: Check that the tests collect.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run ruff and the non-pg suite.**
- [ ] **Step 5: Commit** `feat(api): current_label, label_section_text, trials in SQL`.

### Task 5: FAERS seriousness/outcome table and `faers_counts`

**Files:**
- Modify: `src/episteme/data/regulatory/faers/signals.py` (new table in the signal build), `src/episteme/data/regulatory/faers/generation.py` (table list), `0015_api_layer.sql` (table DDL and function), `lookups.py`
- Test: `tests/data/test_faers_signals.py` (pg, extend it), `tests/reference/test_api_faers.py` (pg)

**Interfaces:**
- Produces:
  - table `episteme.faers_moiety_quarter_outcome(substance_key bigint, quarter date, serious boolean, outcome text, reports bigint, primary key (substance_key, quarter, serious, outcome))`. It counts **distinct reports** whose drug in role `PS` or `SS` resolves to the moiety. `outcome` is each report's outcome code (`DE`, `LT`, `HO`, `DS`, `CA`, `RI`, `OT`, or `none`). `serious` is true when the report has any outcome other than `none`. A report with several outcomes counts once per outcome code.
  - `episteme_api_internal.faers_counts(p_substance_key bigint, p_from date, p_to date) returns setof claim_row`. It returns today's per-quarter and per-term claims (terms as `faers_term` holds them, the PT as published), plus the split claims. The claim text names "reports", and the coverage note says that outcome is per report.
  - The public wrapper (family `faers`).
  - `release_id` is filled from the FAERS `is_latest` release (parked minor).

- [ ] **Step 1: Write the tests.**
  - `test_outcome_table_counts_reports_once`: one report, two reactions, one outcome gives a count of 1.
  - `test_outcome_suspect_only`: a `C` (concomitant) role is not counted.
  - `test_faers_parity_existing_claims`.
  - `test_faers_split_claims_present`.
  - `test_no_meddra_hierarchy_used`: a non-pg test that the SQL text has no `soc`/`hlt`/`llt` table names.
- [ ] **Step 2: Check that the tests collect; run the non-pg test.** Expect FAIL.
- [ ] **Step 3: Implement.** The table is built in the same `build_signals` pass and generation as the other signal tables, so `redo` rebuilds it too.
- [ ] **Step 4: Run ruff and the non-pg suite.**
- [ ] **Step 5: Commit** `feat(faers): seriousness/outcome table and faers_counts in SQL`.

### Task 6: `literature_by_mesh` with topic expansion

**Files:** Modify `0015_api_layer.sql` (function, plus `CREATE INDEX IF NOT EXISTS article_mesh_lower_name_idx ON episteme.article_mesh (lower(descriptor_name))`) and `src/episteme/reference/literature.py`. Test: `tests/reference/test_api_literature.py` (pg).

**Interfaces:**
- Produces: `episteme_api_internal.literature_by_mesh(p_substance_key bigint, p_topic text, p_from_year int default null, p_to_year int default null) returns setof article_row`, and the public wrapper (family `literature`).
  - The topic matches a descriptor by `lower(descriptor_name)`. It is expanded to the descriptors below it in `mesh_tree` (tree-number prefix match).
  - The substance side is today's v1 behaviour.
  - The order is year descending, then `article_id`.
  - `release_id` is filled from the PubMed `is_latest` release (parked minor).
- `literature.literature` keeps its signature. Its claims come from the `article_row` results, mapped in `_api` (`article_rows_to_result`).

- [ ] **Step 1: Write the tests.**
  - `test_literature_parity_without_children`: a topic with no descendants gives the same result as the legacy path.
  - `test_topic_expands_to_descendants`.
  - `test_year_filter`.
  - `test_unknown_topic_note`.
- [ ] **Step 2: Check that the tests collect.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run ruff and the non-pg suite.**
- [ ] **Step 5: Commit** `feat(api): literature_by_mesh with topic expansion`.

### Task 7: `graph_neighbours` (live joins)

**Files:** Modify `0015_api_layer.sql`. Create `src/episteme/reference/graph.py` (Python wrapper `neighbours(conn, article_id, kind, *, tenant, caller) -> LookupResult`). Test: `tests/reference/test_api_graph.py` (pg).

**Interfaces:**
- Produces: `episteme_api_internal.graph_neighbours(p_article_id text, p_kind text) returns setof article_row`, and the public wrapper (family `graph`). The `relation` column is the kind.
  - `cites`/`cited_by`: the existing citation edge table, which `graph_builder` already reads.
  - `trial`: the latest AACT `study_references` rows with `pmid` = the article's PMID. `article_id` is `nct:<nct_id>`.
  - `label`: label articles whose `label_substance` UNII belongs to a substance matched by `norm_name` from the article's `article_chemicals` names.
  - `shared_mesh`: articles sharing the article's major-topic descriptors. The score is the sum, over the shared descriptors, of `1 / count of articles with that descriptor`. Ordered by score descending, then `article_id`.
  - An unknown `p_kind` gives one `note` row listing the allowed kinds. An article that isn't a PMID gives a `note` row for `trial`.

- [ ] **Step 1: Write the tests.**
  - `test_cites_matches_graph_builder`.
  - `test_trial_by_pmid`.
  - `test_label_via_chemicals`.
  - `test_shared_mesh_rare_first`.
  - `test_unknown_kind_note`.
- [ ] **Step 2: Check that the tests collect.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run ruff and the non-pg suite.**
- [ ] **Step 5: Commit** `feat(api): graph_neighbours live joins`.

### Task 8: `lookup_code`

**Files:** Modify `0015_api_layer.sql` and `src/episteme/reference/codes.py`. Test: `tests/reference/test_api_codes.py` (pg).

**Interfaces:** Produces `episteme_api_internal.lookup_code(p_system text, p_code text) returns setof claim_row` and the public wrapper (family `code`). The claim carries the version, OID and licence notice from `code_current`. `codes.lookup_code` keeps its signature.

- [ ] **Step 1: Write the tests.** `test_code_parity` (fixtures as in `test_migration_0013`) and `test_unknown_code_missing_row`.
- [ ] **Step 2: Check that the tests collect.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run ruff.**
- [ ] **Step 5: Commit** `feat(api): lookup_code in SQL`.

### Task 9: Full-text search: index builder and `search_text`

**Files:**
- Create: `src/episteme/data/db/fts_index.py`, `scripts/data/db/fts_index.sh`
- Modify: `0015_api_layer.sql` (function, plus the shared expression function), `config.py`, `.env.example`
- Test: `tests/data/test_fts_index.py` (non-pg and pg), `tests/reference/test_api_search.py` (pg)

**Interfaces:**
- Produces:
  - `episteme_api_internal.fts_doc(p_source text, p_title text, p_abstract text, p_text text) returns tsvector`, `IMMUTABLE`. `to_tsvector('english', ...)` covers title + abstract, or the full `p_text` for sources in (`dailymed`, `ema_epar`).
    - **As built (R3):** `fts_doc(p_source, p_title, p_abstract, p_body_text, p_text_override)` over stored columns (the virtual `text` cannot be indexed); the labels add `body_text`, and `text_override` when it is not NULL or empty. The index expression is `fts_doc(source, title, abstract, body_text, text_override)`. The lock timeout default is 0 with a quiet-database pre-check (R18); the free-space check uses `pg_table_size`; preprints are not covered (their rows are in `article_body_default`).
  - `episteme_api_internal.search_text(p_query text, p_sources text[] default null) returns setof article_row`. It uses `websearch_to_tsquery('english', left(p_query, 1000))`. An empty query, or one of stop words only, gives one `note` row. The rank is `ts_rank_cd`, then `article_id`.
  - The public wrapper (family `search`).
  - The command `python -m episteme.data.db.fts_index build --partition article_body_X [--yes]` and `status`. Partitions allowed: `FTS_PARTITIONS` = pubmed, pmc, openalex, bookshelf, dailymed, ema_epar, plus the preprint partition(s) in `config`. Others are refused (exit 2).
    - The build runs `CREATE INDEX CONCURRENTLY IF NOT EXISTS <partition>_fts_idx ON episteme.<partition> USING gin (episteme_api_internal.fts_doc(source, title, abstract, text))` with `lock_timeout` from the tunable `EPISTEME_FTS_LOCK_TIMEOUT_S` (default 60).
    - Before building, it checks that free space on the data directory's disk is at least 2× the partition's `pg_relation_size`. Otherwise it refuses (exit 1).
  - Preprints: the implementer finds the preprint source name(s) in `article_schema`/`config` and lists them in `FTS_PARTITIONS`. If preprints live in `article_body_default`, they are left out, and the docs say so.

- [ ] **Step 1: Write the tests.**
  - Non-pg: `test_partition_allowlist`, `test_free_space_refusal` (stubbed sizes), `test_build_sql_uses_concurrently_and_identifier`.
  - pg:
    - `test_search_uses_index`: EXPLAIN on the test partition with the index shows `Bitmap Index Scan` on `_fts_idx`.
    - `test_search_hostile_inputs`: quotes, `&|!:*`, a 10,000-character string, an empty string, stop words only; no error, and bounded results.
    - `test_label_full_text_hit`: a word only in a DailyMed body is found.
- [ ] **Step 2: Run the non-pg tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the non-pg tests and ruff.** Expect PASS.
- [ ] **Step 5: Commit** `feat(api): full-text search index builder and search_text`.

### Task 10: Roles, grants and the read-only MCPg profile

**Files:**
- Create: `src/episteme/data/db/grants/0015_api_roles.sql`
- Modify: `scripts/mcpg-episteme.sh` (a `--reader` profile), `config.py` (key name for the reader password), `.env.example` (key name only), `tests/test_mcpg_launcher.py`
- Test: `tests/data/test_api_roles.py` (pg)

**Interfaces:**
- Consumes: every function from Tasks 1–9.
- Produces: roles `episteme_api_owner` (NOLOGIN) and `episteme_api_reader` (LOGIN, the password set by the operator from the `.env` key `EPISTEME_API_READER_PASSWORD`; the SQL file never holds a password). The grants file is idempotent (`DO` blocks for the roles):
  - `ALTER FUNCTION ... OWNER TO episteme_api_owner` for every function in both schemas;
  - `ALTER TABLE` on the settings tables;
  - the owner gets `SELECT` on exactly the base tables the functions read;
  - `REVOKE ALL ON ALL FUNCTIONS IN SCHEMA episteme_api, episteme_api_internal FROM PUBLIC`;
  - the reader gets `USAGE` on `episteme_api` and `EXECUTE` on all functions in `episteme_api`, and nothing else;
  - `ALTER DEFAULT PRIVILEGES` so that future functions in `episteme_api_internal` are not executable by PUBLIC.
- The launcher's `--reader` profile connects as `episteme_api_reader` with restricted mode and DDL off, and refuses `--admin` together with `--reader`.

- [ ] **Step 1: Write the tests.**
  - pg, as a reader created in the test with a random password:
    - `test_reader_cannot_select_base_table`;
    - `test_reader_cannot_execute_internal`;
    - `test_reader_can_call_each_public_function` (smoke, one fixture call each);
    - `test_public_function_in_read_only_transaction` (`SET TRANSACTION READ ONLY`, then a call succeeds);
    - `test_search_path_game_harmless`: the reader sets `search_path` to a schema holding a fake `substance` table; results are unchanged.
  - Non-pg: `test_launcher_reader_profile_env` and `test_launcher_rejects_admin_and_reader`.
- [ ] **Step 2: Run the non-pg tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the non-pg tests and ruff.**
- [ ] **Step 5: Commit** `feat(api): roles, grants file, read-only MCPg profile`.

### Task 11: EPAR offline extraction and EU label substance links

**Files:**
- Modify: `src/episteme/data/ema/extract_epar.py` (`offline_subset` mode), `scripts/data/ema/extract_ema_epar.sh`, the EPAR load path (`scripts/data/ema/load_ema_epar.sh` and its Python loader), adding `label_substance` rows
- Create: `src/episteme/data/ema/epar_links.py`
- Test: `tests/data/test_epar.py` (extend it), `tests/data/test_epar_links.py` (non-pg and pg)

**Interfaces:**
- Produces:
  - `extract_epar(..., offline_subset: bool = False)`, CLI `--offline-subset`. With it, documents whose PDF is absent count as `not_downloaded`, and the run marker gets `"offline_subset": {"present": <n>, "listed": <m>}`. The feed-size and withdrawn-share guards apply unchanged.
    - First the implementer checks whether today's code already runs with absent PDFs and no refusal. If it does, the flag only records the marker. If a guard refuses, the flag scopes that guard to present documents, and only that.
  - `epar_links.link_rows(conn, release_id) -> tuple[list[tuple[str, str]], list[dict]]` returns `(article_id, unii)` link rows plus a report of unlinked documents.
    - The method is `ema_name_exact`, per spec section 6. For each `product-information` document, its product number is looked up in `ema_epar_documents`. That gives the `ema_medicines` record, from which the `active_substance` field is split on `;`. Each part goes through `norm_name` and is matched against `substance_xref` names (`id_type` in `('ema_name', 'name')`).
    - Linked only when exactly one substance matches, it has a UNII, and the medicine's `category = 'Human'`.
    - Unlinked documents (no match, more than one match, veterinary) go in the report with a reason.
  - The loader writes the link rows into `label_substance (article_id, unii, ndc_product NULL)` and writes the report next to the run marker.

- [ ] **Step 1: Write the tests.**
  - Non-pg:
    - `test_offline_subset_marker`: a fixture feed of 5 documents, 2 PDFs present.
    - `test_offline_subset_guards_unchanged_for_full`.
    - `test_split_active_substance`: `'levodopa;carbidopa'` gives two parts.
    - `test_vet_not_linked`.
  - pg:
    - `test_link_exact_single_match`.
    - `test_link_ambiguous_reported`.
    - `test_vaccine_unmatched_reported`.
    - `test_links_idempotent_on_reload`.
  - Keep the `input_key` collision test passing for any new key.
- [ ] **Step 2: Run the non-pg tests.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the non-pg tests and ruff.**
- [ ] **Step 5: Commit** `feat(ema): EPAR offline subset and EU label substance links`.

### Task 12: Generated EU medicine records

**Files:** Create `src/episteme/data/ema/medicine_records.py` and `scripts/data/ema/build_ema_medicine_records.sh`. Test: `tests/data/test_ema_medicine_records.py`.

**Interfaces:**
- Produces: `build_records(conn, processed_dir: Path) -> dict`. It reads the latest `ema_medicines` and `ema_epar_documents` releases from `reg_record` and writes staging parts in the `ema_epar` article row format: `article_id = 'ema_medicine:' || ema_product_number`, `source = 'ema_epar'`, licence as for `ema_epar`.
  - The text comes from a fixed template, with fields in this order: name, active substance, INN, ATC, therapeutic area, category, status, dates (authorisation, withdrawal, last update), indication, then the EPAR documents (title, type, first published, last updated), sorted by type then `first_published_date`. Empty fields are omitted.
  - Lineage names both feed releases.
  - The checkpoint key is the pair of release ids, so a rerun with unchanged releases writes nothing.
  - The output loads through the existing `ema_epar` load path.

- [ ] **Step 1: Write the tests.**
  - Non-pg: `test_template_order_and_omits_empty`, `test_documents_sorted`, `test_article_id_prefix`, `test_rerun_same_releases_noop`, `test_checkpoint_key_collision` (two release pairs never share a key).
- [ ] **Step 2: Run them.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and ruff.** Expect PASS.
- [ ] **Step 5: Commit** `feat(ema): generated EU medicine records for ema_epar`.

### Task 13: DailyMed failure file names, docs and the drift log

**Files:**
- Modify: `src/episteme/data/dailymed/extract_dailymed.py` (`failed_inputs.json` also lists `{file, error}` per failure)
- Modify the docs: `docs/13-graphrag-design.md` (built vs planned, the `episteme_api` function table, MCPg reader instance, pgvector 0.8.7 installed, the semantic-search next spec); `docs/10-data-sources-runbook.md` (migration 0015 plus the grants file, `api_settings sync`, FTS builds one partition at a time with a sample first, EPAR offline subset, medicine records, FAERS outcome table rebuild, the second MCPg instance, and the DailyMed accepted failures); `docs/02-data-sources.md` (EPAR rows); `docs/12-source-inventory.md` (`ema_epar` wired); `docs/project-incubation-baseline.md` (drift entry: landed, rulings, deferred)
- Delete: `tests/reference/_legacy.py`, only after the controller reports that the parity tests passed (the controller tells the implementer in the dispatch)
- Test: `tests/data/test_dailymed_failed_inputs.py` (non-pg), `tests/docs`

- [ ] **Step 1: Write the test** `test_failed_inputs_lists_files` (non-pg).
- [ ] **Step 2: Run it.** Expect FAIL.
- [ ] **Step 3: Implement it and write the docs.**
- [ ] **Step 4: Run the test, `tests/docs` and ruff.** Expect PASS.
- [ ] **Step 5: Commit** `docs: GraphRAG API layer, runbook, drift log; dailymed failure names`.

## Rollout (controller, after merge; one heavy job at a time)

1. Migration 0015 on `episteme_test` (the pg suite), then on `episteme`. `apply_migrations.py` gets `0015_api_layer`.
2. The grants file as superuser on both databases, then `api_settings sync` on both.
3. The FAERS outcome table, built by `build_signals` (sample first).
4. EPAR: extract with `--offline-subset`, then load; then the medicine records, built and loaded; then check the link report.
5. FTS indexes, partition by partition, smallest first (`ema_epar`, `dailymed`, `bookshelf`, `pmc`, `pubmed`, `openalex`), with a free-space check before each.
6. The owner sets up the reader MCPg instance. Smoke test: one call of every public function through it.
