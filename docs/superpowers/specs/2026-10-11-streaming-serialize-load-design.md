# Streaming serialize and load — design

Date: 2026-10-11. Status: approved for execution (full-load run-time fix).

## Problem

The 2026-10-11 full load showed every structured serializer and the loader hold a
whole input in memory, several times over:

- serializers: DuckDB `con.fetchall()` (whole result as Python tuples) → `rows`
  list of dicts → `rows_to_columnar` copy → pyarrow arrays → one `pq.write_table`;
- loader: `pl.read_parquet(...).to_dicts()` (whole shard as dicts), `read_bytes()`
  for the content hash, and every row id passed as one `ANY(%s)` array.

ClinVar (4.68M rows, one input) peaked at ~7.4 GB in serialize and ~10.8 GB in load
on a 16 GB machine, and Claude Code's memory-pressure reaper fired. ChEMBL (~24M
activity rows from one SQLite file) and PubChem cannot finish this way.

## Goals

- Peak memory bounded by a batch size, not by input size, in serialize and load.
- Output and database state identical to today's for the same input
  (same shard path and name, same columns and types, same rows, same audit and
  `_lineage` rows, same delete semantics).
- No partial shard is ever visible to the loader.

## Design

### 1. Streaming staging writer (`src/episteme/data/staging_writer.py`)

- `write_rows(rows, ...)` accepts any `Iterable[dict]` (lists still work) and gains
  `batch_size: int = 50_000`.
- Parquet: a `pq.ParquetWriter` writes one row group per batch, typed by the same
  per-column rules as today (factored into one `_batch_to_table` helper that
  `write_parquet_shard` also uses). The file is written as `<stem>.parquet.tmp` and
  `os.replace`d to `<stem>.parquet` only after the last batch; on any exception the
  temp file is removed and the exception re-raised.
- Zero rows: no file, as today. The return dict is unchanged
  (`format`, `paths`, `n_rows`); `n_rows` is counted while streaming.
- JSONL fallback streams line by line (same temp-and-rename rule).

### 2. Serializers (clinvar, chembl, pubchem, plus the shared pattern elsewhere)

- `process_one` stops building `rows`: it passes a counting generator over
  `iter_rows_from_file` straight to `write_rows`. Counts that used `len(rows)`
  (manifest stats, audit `rows_affected`, `n_rows_partial` on failure) come from
  the counter.
- DuckDB reads use `fetchmany(batch)` in a loop instead of `fetchall()`.
- DuckDB connections set `memory_limit` and `temp_directory` so window/sort
  operators (clinvar's `QUALIFY ROW_NUMBER()`, chembl's joins) spill to disk:
  one helper `episteme.data.duckdb_conn.connect()`; limit from
  `config.get_settings()` (`EPISTEME_DUCKDB_MEMORY_LIMIT`, default `4GB`), temp
  dir under the processed root's `_ops/_duckdb_tmp`.
- `--report` paths may keep their in-memory shape (they are bounded by
  `--max-files`/sample use) but must not regress.

### 3. Streaming loader (`src/episteme/data/postgres_loader.py`)

`load_source_file` keeps one transaction and the same statement semantics:

1. content hash: SHA-256 over the file in 1 MiB chunks;
2. ids: read only the `id` and `source_file` columns (pyarrow `columns=`), COPY
   the ids into a `TEMP` table (`ON COMMIT DROP`), collect distinct `source_file`s;
3. the three deletes run as today, with `= ANY(%s)` over ids replaced by a join
   or `IN (SELECT id FROM <temp>)`;
4. rows stream in batches (`pq.ParquetFile.iter_batches(batch_size)`;
   JSONL: line iterator): per batch, COPY the hot columns into `articles`, then
   the body columns into `article_body`;
5. `_lineage` per `source_file` from counts kept while streaming; audit
   `rows_affected` = total streamed.

## Testing

- Writer: a multi-batch write (`batch_size` smaller than the input) produces a
  table equal to the single-batch write; a generator that raises mid-stream leaves
  no `.parquet` and no `.tmp`; zero rows writes nothing; JSONL fallback likewise.
- Serializers: existing tests pass unchanged; clinvar/chembl/pubchem each get one
  test running `process_one`/`serialize_*` with a tiny batch size and asserting
  the shard equals the default-batch shard.
- Loader (`pg`-marked, run locally against `episteme_test`): multi-batch load gives
  the same `articles`/`article_body`/`_lineage`/audit state as a single batch;
  reload (replace) and id-collision cases keep today's results.
- Memory: a local check loading the real ClinVar shard stays under ~2 GB RSS
  (recorded in the drift log, not a CI test).

## Out of scope

- Changing shard layout (one shard per input stays), partitioning big inputs into
  multiple shards, or parallelising the loader.
- Extractors (pubmed, pmc, bookshelf, …): their inputs are many small files; they
  get the new `write_rows` signature for free and are not reworked.
