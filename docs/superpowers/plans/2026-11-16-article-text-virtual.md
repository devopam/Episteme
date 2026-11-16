# Article Text Virtual Column Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `episteme.article_body.text` stops storing a copy of title + abstract + body: the stored column becomes `text_override` (only where `text` differs from the join), and `text` becomes a VIRTUAL generated column with exactly today's values; a restartable tool nulls the duplicate overrides and REPACKs partition by partition.

**Architecture:** Migration 0014 renames `text` to `text_override` and adds the virtual `text` (metadata only). The loader writes `text_override` (NULL when the shard's `text` equals the join, `''` as the NULL sentinel). A new ops module nulls existing duplicates in bounded batches and runs `REPACK` on one partition at a time.

**Tech Stack:** Python 3.13, psycopg 3, PostgreSQL 19beta3 (virtual generated columns, `REPACK`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-article-text-virtual-design.md` (owner-approved design 2026-11-16; builder-check answers in the spec).

## Global Constraints

- Every reader of `text` sees exactly today's value for every row (anti-goal 1). `text` is NULL exactly when it was NULL.
- Blank part = NULL or `btrim(part, E' \t\r\n') = ''`; Python mirror `p is None or p.strip(" \t\r\n") == ""`. Join separator `E'\n\n'`. The SQL expression is IMMUTABLE (`||` and `CASE` only; `concat_ws` is refused).
- `text_override = ''` means `text` is NULL. NULL override means "use the join".
- No change to the shard contract (docs/09 `text` stays), extractors, serializers, `content_hash` or dedup.
- `config.py` is the only `os.environ` reader; the batch size is a Settings tunable `EPISTEME_TEXT_NULLOUT_BATCH_ROWS` (default 50,000).
- Implementers run only `pytest -m "not pg"` and write pg tests; the controller runs pg tests on `episteme_test`. Nobody touches the `episteme` database or real data.
- One heavy job at a time; REPACK is never run by tests.
- `ruff format`, `ruff check`; URL guard prints nothing; no new dependency; `git add` explicit paths; no `--no-verify`.

## Review Focus

1. A row whose stored `text` is NULL while title/abstract/body is non-blank must still read NULL after 0014 — Task 1 `test_0014_null_text_with_parts_stays_null`.
2. A part that is only an NBSP (`\u00a0`) or other non-listed whitespace is not blank: the join keeps it, and a row whose stored `text` was built without it keeps its override — Task 1 `test_sql_join_matches_python_on_edge_cases` (pg) includes `\u00a0`.
3. A pmc-style row whose stored `text` was whitespace-normalised differently from the plain join must keep its override and read back unchanged — Task 3 `test_nullout_keeps_rows_that_differ`.
4. Re-running the null-out after an interruption must be a no-op on finished batches and leave `text` unchanged — Task 3 `test_nullout_is_restartable`.
5. An openalex load (dedup temp tables made with `LIKE episteme.article_body`) still inserts correctly after the rename — Task 2 covers it through the existing `tests/data/test_openalex_dedup_load.py` and `test_openalex_sweep.py` (controller pg run).

---

### Task 1: Migration 0014, schema.sql, and `sql_join`

**Files:**
- Create: `src/episteme/data/db/migrations/0014_article_text_virtual.sql`
- Modify: `src/episteme/data/db/schema.sql` (article_body definition ~118-140: `text_override text` stored, `text` VIRTUAL with the expression, `SET COMPRESSION` on `body_text` and `text_override`)
- Modify: `src/episteme/ops/gate_migrations.py` (`MIGRATION_MARKERS["0014_article_text_virtual.sql"]`: the `text_override` column exists on `episteme.article_body`, and `text` is a generated column — `attgenerated = 'v'`; follow the 0013 entry's marker style)
- Modify: `src/episteme/data/article_schema.py` (new `sql_join`)
- Test: `tests/data/test_article_schema.py` (or the existing article_schema test file) for `sql_join`; `tests/data/test_migration_0014.py` (pg, new)

**Interfaces:**
- Produces `article_schema.sql_join(title: str | None, abstract: str | None, body_text: str | None) -> str | None` — the non-blank parts (blank rule in Global Constraints) joined by `"\n\n"`; `None` when all are blank. No whitespace normalisation (it mirrors SQL, not `build_text`).
- Produces `article_schema.TEXT_JOIN_SQL: str` — the SQL `<join>` expression over columns `title`, `abstract`, `body_text` (used by 0014, schema.sql via copy, and Task 3's null-out predicate; one Python constant, and a non-pg test asserts the .sql files contain it verbatim).
- 0014 body: `SET LOCAL lock_timeout = '60s'`; skip when `text_override` already exists (DO block); `ALTER TABLE episteme.article_body RENAME COLUMN text TO text_override;` `UPDATE episteme.article_body SET text_override = '' WHERE text_override IS NULL AND <any part non-blank>` (the sentinel; sampled 0 rows); `ALTER TABLE ... ADD COLUMN text text GENERATED ALWAYS AS (CASE WHEN text_override = '' THEN NULL ELSE coalesce(text_override, nullif(<join>, '')) END) VIRTUAL;` One transaction.

- [ ] **Step 1 (non-pg):** `test_sql_join_*`: (`"T"`,`"A"`,`None`) → `"T\n\nA"`; all `None` → `None`; (`"  "`,`"\t"`,`"B"`) → `"B"`; `"\u00a0"` is not blank; no normalisation (`"a  b"` stays). `test_sql_files_carry_the_join_expression`: `TEXT_JOIN_SQL` occurs verbatim in 0014 and schema.sql.
- [ ] **Step 2:** run, FAIL; implement `sql_join`, `TEXT_JOIN_SQL`, 0014, schema.sql, gate markers; run, PASS.
- [ ] **Step 3 (pg tests, written not run), `tests/data/test_migration_0014.py`:** `schema.sql` now has the final shape, so the pre-0014 state is built in a scratch schema (ruling): the test creates schema `t0014` with an `article_body` table LIST-partitioned by `source` in the OLD shape (`article_id, source, year, title, abstract, body_text, text`, two partitions), inserts fixtures, runs the 0014 SQL text with `episteme.article_body` replaced by `t0014.article_body` (0014 must reference the table only as `episteme.article_body`, so the substitution is exact), drops the schema at the end, and asserts:
  - `test_0014_text_unchanged_for_every_shape` (literature equal-to-join row, pmc-style different row, structured text-only row, all-empty row) — `text` after = `text` before;
  - `test_0014_null_text_with_parts_stays_null` (sentinel);
  - `test_0014_is_idempotent` (second run: no error, no change);
  - `test_sql_join_matches_python_on_edge_cases` (SELECT the SQL `<join>` on VALUES rows incl. `\u00a0`, tabs, all-NULL; compare with `sql_join`).
- [ ] **Step 4:** fix every test/fixture that INSERTs into `episteme.article_body (... text ...)` to write `text_override` (found 2026-11-16: `test_corpus_materializer.py:92`, `test_migration_0007.py:29`, `test_migration_0009.py:29`, `test_migration_0012.py:33`, `test_postgres_loader.py:314,319,328,533`, `test_retire_source_files.py:61`, `tests/ops/test_gate_report.py:235,240`; `grep -rn "article_body" tests` for any other). `test_corpus_exclusion.py:24` uses DuckDB with its own table: leave it.
- [ ] **Step 5:** `pytest -m "not pg" -q tests/data tests/ops tests/docs`; ruff; commit `feat(db): migration 0014 — article_body.text as a virtual column`.

### Task 2: Loader writes `text_override`

**Files:**
- Modify: `src/episteme/data/postgres_loader.py` (`_TEXT_COLUMNS`, `BODY_COLUMNS` ~64-76, `_batch_tuples` ~281-310, the dedup temp-table INSERT ~582 and the body COPY ~941)
- Test: `tests/data/test_postgres_loader_columns.py` (non-pg)

**Interfaces:**
- Consumes `article_schema.sql_join`.
- `BODY_COLUMNS` names `text_override` instead of `text`. Per row: `text_override = None if shard_text == sql_join(title, abstract, body_text) else ("" if shard_text is None else shard_text)` — i.e. NULL when equal (including both NULL), `''` when the shard text is NULL but the join is not, else the shard text.
- The dedup temp tables (`CREATE TEMP TABLE ... (LIKE episteme.article_body)`) inherit the new shape; the INSERT … SELECT uses `BODY_COLUMNS` (never `text`). If `LIKE` copies the virtual column as a virtual column, that is fine; if as a plain column, it is ignored.

- [ ] **Step 1 (non-pg):** `test_body_columns_write_text_override_not_text`; `test_override_is_null_when_text_equals_the_join`; `test_override_is_text_when_it_differs` (pmc-style `"T\n\nA  B"` vs parts); `test_override_is_empty_sentinel_when_text_null_and_parts_present`; `test_override_null_when_all_null`.
- [ ] **Step 2:** run, FAIL; implement; run, PASS.
- [ ] **Step 3:** `pytest -m "not pg" -q tests/data`; ruff; commit `feat(load): write article_body.text_override (NULL when text equals the join)`.
- [ ] **Step 4:** the controller runs the pg suites that load into `article_body` on `episteme_test` (loader, dedup, sweep, retire, corpus materializer, gate report, migrations 0007-0014).

### Task 3: Null-out and REPACK tool

**Files:**
- Create: `src/episteme/data/db/text_override.py` (CLI `python -m episteme.data.db.text_override`)
- Create: `scripts/data/db/text_override.sh` (thin wrapper, `load_dotenv`, like the other db wrappers)
- Modify: `src/episteme/config.py` (`text_nullout_batch_rows: int`, `EPISTEME_TEXT_NULLOUT_BATCH_ROWS`, default 50,000)
- Test: `tests/data/test_text_override_tool.py` (non-pg: argument checks, SQL text; pg: written, controller runs)

**Interfaces:**
- `nullout(conn, partition: str, *, batch_rows: int, log) -> dict` — `partition` must be an existing `episteme.article_body_<source>` partition (validated against `pg_inherits`, name regex `^article_body_[a-z0-9_]+$`). Walks the partition in primary-key order (`article_id, source`) in batches of `batch_rows`, one transaction per batch: `UPDATE ... SET text_override = NULL WHERE <pk in batch> AND text_override IS NOT NULL AND text_override <> '' AND text_override = <TEXT_JOIN_SQL>`; commits; logs progress (rows scanned, rows nulled). Restartable: the WHERE makes a repeated batch a no-op; a `--start-after` key may resume. Returns `{"rows": n, "nulled": m}`.
- `repack(conn, partition: str) -> None` — `REPACK episteme.<partition>` (autocommit; PG 19). Refuses unless `--yes-repack` is given on the CLI.
- `report(conn) -> list[dict]` — per article_body partition: rows (reltuples), override NULL count (sampled `TABLESAMPLE SYSTEM (0.1)` estimate is acceptable, say so), `pg_total_relation_size` — printed as a table; writes nothing.
- `precheck(conn, partition: str, *, batch_rows: int) -> dict` (read-only, no lock beyond row reads, batched by primary key): counts `text IS NULL AND a part is non-blank` and `text = ''` (before 0014 the column is `text`; after 0014 use `text_override`). Both must be 0 before 0014 is applied to a database (controller ruling 2026-11-16: 0014 has no data UPDATE).
- CLI: `precheck --partition NAME|--all`, `report`, `nullout --partition NAME [--batch-rows N]`, `repack --partition NAME --yes-repack`. Refuses to run when `server_version` is not `19beta3`. Logs to stdout (the operator tees to `D:/episteme/_runlogs/...`).

- [ ] **Step 1 (non-pg):** partition-name validation refuses `article_body; drop`, `articles_pubmed`; batch size from Settings; repack without `--yes-repack` exits 2.
- [ ] **Step 2 (pg, written not run):** `test_nullout_nulls_only_equal_rows`, `test_nullout_keeps_rows_that_differ`, `test_nullout_keeps_the_null_sentinel`, `test_nullout_is_restartable` (run twice; second run nulls 0), `test_text_unchanged_after_nullout` (every fixture row: `text` before = after). REPACK is not run in tests.
- [ ] **Step 3:** implement; `pytest -m "not pg" -q tests/data/test_text_override_tool.py tests/test_config_tunables.py`; ruff; commit `feat(db): restartable null-out and REPACK of duplicate article text`.

### Task 4: Docs and drift log

**Files:**
- Modify: `docs/09-extraction-contract.md` (shards keep `text`; in the database `text` is virtual from `text_override` + the join; the sentinel)
- Modify: `docs/10-data-sources-runbook.md` (migration table: 0014; a rollout section "Article text without duplication": apply 0014 with no load running; `report`; `nullout` then `repack` for dailymed, then pmc, then — owner go — pubmed, then — owner go — openalex; one heavy job at a time; sizes into the drift log)
- Modify: any storage doc the `tests/docs` suite checks for `article_body` columns (`grep -rn "body_text, text\|article_body" docs/*.md`)
- Modify: `.env.example` (`EPISTEME_TEXT_NULLOUT_BATCH_ROWS`, commented default)
- Modify: `docs/project-incubation-baseline.md` (dated entry: what landed, the sampling numbers from the spec, rulings, deferred: sizes after REPACK)

- [ ] **Step 1:** write the docs (STE style).
- [ ] **Step 2:** `pytest -m "not pg" -q tests/docs`; URL guard; commit `docs: article text as a virtual column (0014, null-out, REPACK)`.
