# Pipeline throughput on fixed hardware — design

Date: 2026-10-13. Status: design approved by the user in chat; this spec awaits review.

## Problem

The full load runs on one laptop: 4 cores / 8 threads (i7-10510U), 16 GB RAM, data on a
5,400 rpm USB HDD (D:), and an internal NVMe SSD (C:). Measurements from the 2026-10-12/13
load:

- **Extract is limited to one core.** The pubmed extract ran at about 4.4 files/min with the
  machine at 14% total CPU and the data disk 10% busy. Every extractor runs its per-file
  `process_one` through `ThreadPoolExecutor`, but parsing XML and JSON in Python is
  CPU-bound, so Python's global interpreter lock (GIL) keeps it to about one core. `--workers`
  also defaults to 1.
- **The load is limited by the database write path.** chembl loaded at about 3,100 rows/s
  (24.5M rows in about 2.2 h of running time). Part of the cost is per-row Python work
  (`to_pylist()` dicts, then a tuple per row for `write_row`). Part is index upkeep:
  `articles_default` has 9 indexes, including three GIN indexes (`mesh`, `authors`,
  `publication_types`) that pubmed rows (about 36M) fill, unlike chembl's empty arrays.

Done separately, outside this spec (PR #46 and operator steps): PostgreSQL bulk-load settings
(`tune_postgres.sh`), and `pg_wal` moved to the NVMe SSD.

## Goals

1. Extractors and the openalex serializer use several cores. Target: the pubmed extract at
   least 2.5× faster than 4.4 files/min, with byte-for-byte identical shards.
2. Faster gzip decompression wherever the pipeline reads `.gz` in Python, with identical
   output.
3. The loader records where its time goes, per shard, and loses the per-row dict building.
   Database state stays identical.
4. A bulk-load mode drops the secondary indexes of `episteme.articles` for a large load and
   always rebuilds them afterwards, with the definitions unchanged and every step audited.

## Non-goals

- Rust, free-threaded Python, or a full Arrow-to-`COPY` encoder. That encoder is reconsidered
  only if goal 3's measurements show the client is still the bottleneck.
- Migrating `curate/serialize_structured_sources.py` from pandas (dead code, never called by
  the pipeline).
- The DuckDB serializers (chembl, clinvar, pubchem, reactome; DuckDB already uses every
  core), single-file parsers (mesh, ontologies, cdisc_ct; nothing to parallelise per file),
  and downloads (network waits, where threads are right).

## Design

### 1. Process-based per-file workers

New module `src/episteme/data/parallel.py`:

```python
def run_per_file(
    fn: Callable[..., dict[str, Any]],
    files: Sequence[Path],
    *,
    workers: int,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
```

- `workers == 1`: calls `fn(path, **kwargs)` in order in this process (today's
  single-threaded path, unchanged).
- `workers > 1`: a `concurrent.futures.ProcessPoolExecutor(max_workers=workers,
  mp_context=multiprocessing.get_context("spawn"))`. Spawn is the only start method on
  Windows and is chosen explicitly so Linux CI behaves the same way. Results are collected
  with `as_completed`, and `on_result` is called in the parent for each one (verbose
  printing).
- `fn` must be a module-level function, and all arguments must be picklable (`Path`, `bool`,
  `int`, `str`). Each of the eight modules already has a module-level `process_one(path, *,
  ...)` taking such arguments.
- A worker exception that escapes `fn` (every `process_one` already catches its own and
  writes a failed marker, so this means a crash or a pickling error) becomes a result
  `{"source_file": <input_key>, "skipped": False, "ok": False, "error": "worker: <repr>"}`.
  The run then continues, as one file failing does today. A worker process that dies
  (`BrokenProcessPool`) fails the remaining files the same way and is logged once.
- Children inherit the environment (`EPISTEME_RUN_ID`, `.env` values already exported by the
  wrapper), so `get_settings()` gives the same run id in every worker.

Callers: `extract_pubmed`, `extract_pmc`, `extract_bookshelf`, `extract_guidelines`,
`extract_apollo`, `extract_europepmc_preprints`, `extract_europepmc_manuscripts`, and
`serialize_openalex`. Each replaces its `if workers == 1 ... else ThreadPoolExecutor` block
with one `run_per_file(process_one, files, workers=workers, on_result=..., raw_dir=...,
processed_dir=..., force=...)` call, passing exactly the keyword arguments it passes today.
Summaries, run manifests and return values are unchanged.

**Worker count.** A new `Settings.extract_workers: int` reads `EPISTEME_EXTRACT_WORKERS`
(default 3: the physical cores minus one, leaving a core for PostgreSQL and the operating
system; about 400 MB per worker). A non-integer or a value below 1 falls back to 3 with a
logged warning, as other settings do. `--workers N` on each CLI still overrides it; the
CLI's default becomes `None`, meaning "use the setting". `config.py` stays the only module
that reads `os.environ`.

**Audit and markers in workers.** `process_one` writes its shard (temp file, then rename),
its success or failed marker, and its `extract_commit` audit row, all inside the worker, as
today. The audit chain is safe across processes: `audit_trail.record` takes
`pg_advisory_xact_lock(hashtext('episteme._audit'))` before reading the previous hash. The
module-level `_AUDIT_LOCK` thread locks stay; they are harmless, and the advisory lock is the
real serialisation. Each worker opens its own connection per audit write, as each thread does
today.

**Ordering.** Extract order does not decide load order: the loader discovers shards itself.
pubmed's baseline-before-updates sort in `discover_pubmed_files` stays, and with workers > 1
it decides only the submission order, exactly as it does with threads today.

### 2. Faster gzip

- Add `isal` (python-isal) to the `data` extra.
- New helper `episteme.data.gz.open_gz(path, mode="rb", **kw)`: returns
  `isal.igzip.open(...)` when `isal` imports, else `gzip.open(...)`, with the same
  signature and text-mode keywords.
- Use it in `extract_pubmed.iter_rows_from_file`, `graph_builder` (the pubmed graph re-reads
  the XML), `serialize_openalex`, `serialize_mesh` and `serialize_uniprot`. Other `gzip.open`
  users stay as they are (writers, or one-off small files).

### 3. Loader: column-wise rows and timing

In `postgres_loader.load_source_file`:

- `_iter_batches` yields, per Arrow record batch, the columns as Python lists
  (`rb.column(name).to_pylist()` once per needed column) and the loop zips them into the
  `write_row` tuples. That replaces `rb.to_pylist()` (a dict per row) followed by a tuple per
  row. The JSONL fallback keeps its dict path (it is a fallback, not the hot path).
  `_hot_value`, `year` coercion and every other per-value rule are applied unchanged, to the
  same values.
- Per shard, the loader measures wall time spent (a) reading and converting batches and (b)
  inside `COPY` writes, and adds `read_sec`, `copy_sec`, `rows_per_sec` to the dict it already
  returns. `load_articles` prints them on the `ok ... event=load_commit` line. They are not
  added to the audit row (the audit schema is unchanged).

### 4. Bulk-load mode

**Migration `0005_bulk_index_state.sql`** (idempotent, applied by hand like 0002-0004):

```sql
CREATE TABLE IF NOT EXISTS episteme._bulk_index_state (
    index_name  text PRIMARY KEY,      -- schema-qualified, parent index on episteme.articles
    definition  text NOT NULL,         -- pg_get_indexdef() of the parent index
    run_id      text,
    dropped_at  timestamptz NOT NULL DEFAULT now()
);
```

Owned by `episteme_sys_admin`; no grant to `episteme_app`.

**Indexes covered** (parent indexes on the partitioned `episteme.articles`; dropping a parent
drops every partition's index): `articles_pmid_idx`, `articles_pmcid_idx`, `articles_doi_idx`,
`articles_mesh_gin`, `articles_authors_gin`, `articles_publication_types_gin`. Kept: primary
keys, `articles_source_file_idx` and `articles_id_idx` (both used by the loader's deletes),
and `articles_retrieved_at_brin` (cheap to maintain). The list is a constant in one place and
is checked against the catalog: a name that does not exist is an error, not a skip.

**Parent definitions read `ON ONLY`.** On a partitioned table, `pg_get_indexdef()` of a
parent index returns `CREATE INDEX <name> ON ONLY episteme.articles ...` (checked on the live
catalog, 2026-10-13). Re-running that text would create an empty, invalid parent index with
no partition indexes. So `rebuild` must not run the saved text verbatim. It removes exactly
one `ON ONLY ` (the definition must match
`^CREATE INDEX \S+ ON ONLY episteme\.articles USING `; otherwise `rebuild` stops with an
error) and runs `CREATE INDEX <name> ON episteme.articles ...`, which builds the index on
every partition and attaches them. The partition index names are generated as before
(`<partition>_<columns>_idx`).

**Script `scripts/data/db/bulk_load_indexes.sh drop|rebuild|status`**, run as
`episteme_sys_admin`, the role that owns the tables and can drop and create their indexes:

- `drop`: refuses (exit 2) if `_bulk_index_state` already has rows, because a previous
  rebuild never finished. In one transaction: insert `pg_get_indexdef()` for each covered
  index, `DROP INDEX` each, and write a `bulk_index_drop` audit row (object = the index
  names, `run_id` = `EPISTEME_RUN_ID`).
- `rebuild`: for each saved row, it runs the saved definition with `ON ONLY` turned into `ON`
  (above), and skips the row when an index of that name already exists and is valid. Each
  index is built in its own transaction, so a failure keeps the indexes already rebuilt. The row is deleted in the same transaction that
  rebuilds its index. Then `ANALYZE episteme.articles` and one `bulk_index_rebuild` audit row.
  With no saved rows it does nothing (exit 0). Idempotent: safe to re-run after a failure.
- `status`: prints the saved rows; exit 0 if none, 1 if any (a rebuild is pending).
- The audit rows go through the project's audit writer (Python
  `episteme.audit_trail.record`), not hand-written SQL. The script calls a small Python entry
  point (`python -m episteme.data.db.bulk_indexes drop|rebuild|status`) that does the catalog
  work and the audit in the same transaction. The shell script is the operator-facing
  wrapper, like the other `db/*.sh`.

**`run_pipeline.sh <source> load --bulk`:**

1. `bulk_load_indexes.sh drop`. If it fails, the load does not start.
2. Run the load stage exactly as today.
3. Always `bulk_load_indexes.sh rebuild`, whether the load succeeded, failed or was
   interrupted (a `trap` on EXIT, INT and TERM in `run_pipeline.sh`). The exit code is the
   load's if the load failed, else the rebuild's.
4. `--bulk` is accepted only for the `load` stage. Elsewhere it exits 2 with a usage message.

If the process is killed hard (no trap runs), the saved state survives:
- the next `--bulk` refuses to start;
- `bulk_load_indexes.sh status` shows the pending rebuild;
- `bulk_load_indexes.sh rebuild` completes it.

The runbook says this.

**Role and guard.** Dropping and creating indexes needs the table owner,
`episteme_sys_admin`. The runtime role `episteme_app`, which `dsn_from_settings()` uses,
cannot do it. So:
- `Settings` gains `sys_admin_password`, read from `EPISTEME_SYS_ADMIN_PASSWORD` (the key
  the `db/*.sh` scripts already use; `config.py` stays the only module that reads the
  environment).
- `db/connection.py` gains `admin_dsn_from_settings()`: it runs the same
  `check_database_allowed(settings)` guard first, then builds the DSN from the same host,
  port and database with user `episteme_sys_admin` and that password.
- The entry point opens one plain connection (not the pool) with `pool_kwargs(settings)`,
  so `read-only` mode's read-only session option applies and `--bulk` fails there as any
  write would.
- The DB-mode guard (`EPISTEME_DB_MODE`) therefore applies exactly as for every other
  production operation.

### 5. Documentation

- `docs/10-data-sources-runbook.md`:
  - `EPISTEME_EXTRACT_WORKERS` in the env-key table;
  - migration 0005 in section 2.3;
  - a "Bulk-load mode" subsection (when to use `--bulk`, what it drops, what to do after a
    hard kill: `status`, then `rebuild`);
  - the loader's new timing fields.
- `.env.example`: `EPISTEME_EXTRACT_WORKERS` (commented, default 3).
- `docs/08-data-storage-principles.md`: `_bulk_index_state`, wherever the operational tables
  are listed.
- `docs/11-gxp-data-integrity.md`: the `bulk_index_drop` and `bulk_index_rebuild` audit
  events.
- Drift log: an entry for this branch.

## Testing

Ordinary tests (`not pg`, run in CI):

- `parallel.run_per_file`:
  - with workers 1 and with workers 3, it returns the same set of results for a module-level
    test function;
  - a worker exception becomes an `ok: False` result and the other files still run;
  - `on_result` runs in the parent.
- `Settings.extract_workers`: the default is 3, `EPISTEME_EXTRACT_WORKERS=5` gives 5, and
  invalid values fall back to 3. `Settings.sys_admin_password` reads
  `EPISTEME_SYS_ADMIN_PASSWORD`. `admin_dsn_from_settings()` uses `episteme_sys_admin`
  and runs the guard (a database the guard refuses raises before any connection).
- `open_gz`: the decompressed bytes are identical with `isal` and with `gzip` forced (a
  monkeypatch hides `isal`), in both binary and text mode.
- Each converted extractor: on its existing fixture inputs, the shards produced with
  `workers=1` and with `workers=3` are identical (rows compared after sorting by `id`).
- Loader: on a fixture shard, the tuples sent to `write_row` by the column-wise path equal
  those from the dict path (the dict path kept as a test helper).
- `run_pipeline.sh`:
  - `--bulk` on a non-load stage exits 2;
  - with fake `bulk_load_indexes.sh` and load wrappers, `rebuild` runs after a load that
    succeeds, after one that fails, and after one that is interrupted, and the exit code
    follows rule 3.

`pg` tests (on `episteme_test`):

- `drop`, then `rebuild`: the `pg_get_indexdef()` of every covered index is identical before
  and after, on the parent and on every partition (names included). Every parent is
  `indisvalid` with one attached index per partition, and `_bulk_index_state` is empty.
- `rebuild` refuses a saved definition that does not match the `ON ONLY episteme.articles`
  pattern.
- `drop` with rows already saved exits 2 and drops nothing.
- A `rebuild` interrupted after the first index (simulated by a failing definition) leaves
  the rest saved, and a second `rebuild` finishes them.
- Audit rows `bulk_index_drop` and `bulk_index_rebuild` are written, and the chain verifies.
- Load equivalence: the same shard loaded with the old and the new loader gives identical
  `articles` and `article_body` rows.

## Rollout

1. Merge, then apply migration 0005 to `episteme_test`, run the `pg` tests, then apply it to
   `episteme` (no load running).
2. Resume Stage 2: `pubmed extract` continues from 373 of 1,334 done files with 3 workers.
   Measure files/min against 4.4.
3. `pubmed load --bulk`: record rows/s and the `read_sec`/`copy_sec` split per shard, then the
   rebuild time.
4. Record the measurements in the drift log. Decide on the full Arrow-to-`COPY` encoder only
   from them.
