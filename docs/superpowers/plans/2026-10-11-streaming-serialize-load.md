# Streaming serialize and load — implementation plan

Spec: `docs/superpowers/specs/2026-10-11-streaming-serialize-load-design.md` (binding).

Goal: bound peak memory in structured serializers and the Postgres loader by a
batch size instead of by input size, with byte-for-byte equivalent outputs and
database state.

## Global Constraints

- `src/episteme/config.py` is the only module that reads `os.environ`.
- Endpoint URLs live only in `scripts/data/_lib/sources.env` (no URLs added anywhere).
- Shard path and name stay `<processed_root>/staging/<source>/<stem>.parquet`
  (same stem sanitising as today). Columns and Arrow types stay exactly as
  `write_parquet_shard` produces today.
- A shard is visible under its final name only when complete: write to
  `<final>.tmp`, `os.replace` at the end, remove the temp file on any exception.
- Checkpoint markers, `input_key`, manifests and audit events keep today's
  shapes and values; counts come from counters instead of `len(rows)`.
- Default batch size: 50_000 rows (one module-level constant per module, overridable
  by a keyword argument for tests).
- DuckDB memory limit: `Settings.duckdb_memory_limit`, env key
  `EPISTEME_DUCKDB_MEMORY_LIMIT`, default `"4GB"`; temp dir
  `<processed_root>/_ops/_duckdb_tmp` (created on demand).
- Run `ruff format` and `ruff check` on changed Python; `pytest -m "not pg"`
  must pass; `pg` tests run locally with `PGDATABASE=episteme_test` and `.env`
  sourced (`set -a; . ./.env; set +a`) — never read or print `.env`.
- `git add` explicit paths only; never `--no-verify`. Commit trailer:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z`.

## Task 1: Streaming staging writer

Files: `src/episteme/data/staging_writer.py`, `tests/data/test_staging_writer.py`.

- `write_rows(rows: Iterable[dict[str, Any]], staging_root, *, source, source_file,
  prefer_parquet=True, batch_size: int = WRITE_BATCH_ROWS)` with
  `WRITE_BATCH_ROWS = 50_000`. Return dict unchanged:
  `{"format", "paths", "n_rows"}`.
- Factor today's per-column typing into `_batch_to_table(rows: list[dict]) -> pa.Table`
  with an explicit schema so every batch has an identical schema (a batch whose
  list column is all-None must still be `list<string>`, `year` int32, the three
  booleans bool, the rest string). `write_parquet_shard(rows: list, ...)` keeps its
  signature and uses the helper (single batch).
- Parquet streaming: `pq.ParquetWriter(tmp_path, schema, compression="zstd")`,
  one `write_table` per batch; `os.replace(tmp, final)` after the last batch;
  zero rows → no file, `paths == []`, `n_rows == 0` (today's behaviour).
- pyarrow missing → JSONL fallback, streamed line by line, same temp-and-rename.
- Tests: multi-batch (`batch_size=2` over ≥5 rows, including a row whose list
  columns are None and one whose year is None) equals single-batch
  (`pq.read_table(...).equals(...)`) and equals `write_parquet_shard` output;
  a generator that raises after 3 rows → exception propagates, no `.parquet`, no
  `.tmp` left; empty iterator writes nothing; generator input works (not only lists).

## Task 2: DuckDB connection helper and setting

Files: `src/episteme/config.py`, `src/episteme/data/duckdb_conn.py` (new),
`tests/data/test_duckdb_conn.py` (new), `docs/10-data-sources-runbook.md`
(one line naming the env key and default in the existing env/settings section,
no values from `.env`).

- `Settings.duckdb_memory_limit: str` from `EPISTEME_DUCKDB_MEMORY_LIMIT`,
  default `"4GB"`.
- `duckdb_conn.connect(*, memory_limit: str | None = None, temp_dir: Path | None = None)
  -> duckdb.DuckDBPyConnection`: in-memory connection; `SET memory_limit`
  (from argument or settings), `SET temp_directory` (argument or
  `<processed_root>/_ops/_duckdb_tmp`, `mkdir(parents=True, exist_ok=True)`),
  `SET preserve_insertion_order = false` is NOT set (ordering matters to
  callers). Values are passed safely (validate memory_limit against
  `^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$`, case-insensitive; raise
  `ValueError` otherwise — no SQL injection through the setting).
- `fetch_batches(con, batch_size=FETCH_BATCH_ROWS) -> Iterator[list[tuple]]`
  helper looping `con.fetchmany(batch_size)` until empty; `FETCH_BATCH_ROWS = 50_000`.
- Tests: settings default and override (via the project's existing settings-test
  idiom); `connect(memory_limit="512MB", temp_dir=tmp)` → `current_setting('memory_limit')`
  reflects it and temp dir exists; bad limit raises; `fetch_batches` over a
  `range(7)` query with batch 3 yields 3,3,1.

## Task 3: Stream clinvar, chembl and pubchem serializers

Files: `src/episteme/data/{clinvar,chembl,pubchem}/serialize_*.py` and their tests
under `tests/data/`.

- Each `iter_rows_from_file` uses `duckdb_conn.connect()` instead of
  `duckdb.connect()` and `fetch_batches` instead of `fetchall()`, yielding rows
  per batch (still a generator; same row order and content). Keep the
  connection open until the generator finishes (`try/finally: con.close()`).
  Update the docstrings that say "NOT a streaming read".
- Each `process_one` passes a counting generator straight to `write_rows`
  (no `rows` list). Every former `len(rows)` (manifest `n_rows`, audit
  `rows_affected`, `n_rows_partial` on failure) uses the counter. On failure
  mid-stream the writer leaves no shard (Task 1), the failed marker records the
  rows counted so far.
- `--report` paths: keep behaviour; if they build a list, leave them.
- Tests per source: serialize the existing fixture with a tiny batch size
  (monkeypatch `duckdb_conn.FETCH_BATCH_ROWS` and/or pass `batch_size` through
  to `write_rows` via a module constant) and assert the shard equals the default
  run's shard; existing tests unchanged and green.

## Task 4: Same counting-generator pattern in the remaining serializers

Files: `src/episteme/data/{mesh,ontologies,reactome,uniprot,cdisc_ct,openalex}/serialize_*.py`.

- Mechanical: replace the `rows` list in each `process_one` with the counting
  generator into `write_rows`, counts from the counter. reactome also switches
  its DuckDB use to `duckdb_conn.connect()` + `fetch_batches`. No behaviour change;
  existing tests must pass unchanged. No new tests required beyond the existing
  ones (they cover output equality).

## Task 5: Streaming Postgres loader

Files: `src/episteme/data/postgres_loader.py`, `tests/data/test_postgres_loader.py`
(and `tests/data/test_load_articles.py` only if signatures it uses change).

- `load_source_file(conn, *, source, staging_path, run_id, batch_size=LOAD_BATCH_ROWS)`,
  `LOAD_BATCH_ROWS = 50_000`; same return dict and same statements' semantics:
  1. content hash: SHA-256 streamed in 1 MiB chunks (same hex as before);
  2. ids/source_files: Parquet → `pq.read_table(path, columns=["id", "source_file"])`
     (JSONL → one streamed pass); distinct source_files sorted; ids COPYed into
     `CREATE TEMP TABLE _load_ids (id text) ON COMMIT DROP`;
  3. the three deletes as today, with every `= ANY(%s)` over ids replaced by
     `IN (SELECT id FROM _load_ids)`; per-source_file delete counts unchanged;
  4. stream rows: Parquet `pq.ParquetFile(path).iter_batches(batch_size)` →
     `to_pylist()`; JSONL line iterator in chunks; per batch COPY hot columns into
     `episteme.articles` then body columns into `episteme.article_body`
     (same coercions `_hot_value`/`_coerce_year`);
  5. `_lineage` rows from per-source_file counters; audit `rows_affected` = total.
- Keep `_read_shard` if other modules import it; otherwise it may stay for
  compatibility — do not delete public helpers used elsewhere (grep first).
- Tests (`pg`-marked, existing idiom in the file): a shard of ≥7 rows loaded with
  `batch_size=3` gives the same `articles`, `article_body`, `_lineage` and audit
  rows as `batch_size=50_000`; a reload replaces (event `load_replace`, counts as
  today); the id-collision case (same id, different source_file) behaves as today.
  Run them locally against `episteme_test`.

## Task 6: Drift log

File: `docs/project-incubation-baseline.md`.

- Add an entry (2026-10-11) to the drift log: full-load memory defect (clinvar
  serialize ~7.4 GB, load ~10.8 GB peak; Claude Code reaper fired); the streaming
  writer/loader and DuckDB memory limit landed; new env key
  `EPISTEME_DUCKDB_MEMORY_LIMIT` (default `4GB`); extractors untouched (many small
  inputs). Key names only, no values.
- Same entry also records the other 2026-10-11 full-load findings (facts supplied
  in the dispatch): new 4 TB Toshiba drive passed a sampled capacity test and PG19
  was restored onto it; reactome `ReactomePathwaysRelation.txt` mis-discovered as a
  primary (PR #41); Windows aria2c TLS handshake failure against ftp.expasy.org
  with curl fallback (PR #42); Claude Code's memory-reaper notice again reported the
  stage driver killed while it kept running; aact download URL returns 404 (open).
