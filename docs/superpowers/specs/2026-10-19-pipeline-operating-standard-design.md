# Pipeline operating standard (sub-project 1)

Date: 2026-10-19. Status: approved in conversation, section by section; this file is for review.

## 1. Purpose

The data pipeline must run reliably on the current laptop and get faster on larger hardware with no code change. Every stage (download, extract, serialize, load, graph, enrich) follows one operating standard:

- no single unit of work can lose hours when it fails;
- a re-run continues where the last run stopped;
- parallelism, memory caps and batch sizes come from the machine, and an operator can override each one;
- one heavy disk or database job runs at a time;
- a new or changed stage passes a sample run with quality checks before its full run;
- every run reports its progress in one place, and a stalled run is detected.

This spec is sub-project 1 of 4:

1. this operating standard and its shared components;
2. `pubchem` as the first application (bounded parts, a streaming merge join), as its own spec;
3. an audit of the other stages against this standard, as small PRs;
4. data analysis and modelling for new sources (openFDA, Drugs@FDA), later.

## 2. Context and evidence (2026-10-18 / 19)

- The laptop has 4 physical cores and 16 GB of RAM. The data root is on one 5,400 rpm USB HDD (D:), and C: is an internal SSD.
- Parallel jobs on D: drove the disk queue to 18, with 0.25 to 4 s per write. The PMC download fell from 139 to 17 IDs/s, and loads stalled. With the downloads paused, the same purge took 32 min, against 2.5 h.
- The `pubchem` serializer wrote one 120-million-row shard. That would be one load transaction of about 40 h. Its DuckDB join spilled 60 GB.
- The loader logged a failed shard and went on; the error showed only at the end.
- Values that should depend on the machine are fixed: extract workers 3, DuckDB 4 GB, PMC threads 48, batch rows 50,000, `shared_buffers` 2 GB.
- Run scripts in a scratch folder caused false successes: Git Bash reports exit 0 for a killed child bash, and a running script was edited.
- Progress was read in a different way for each stage. A hung statement was not obvious for 3 h.

## 3. Principles (from the user)

1. Bounded units of work: split big inputs into parts, each with its own identity.
2. Fail fast, and resume where the run failed.
3. Throughput from parallelism, sized to the hardware, parameterized and auto-detected, within stable limits.
4. A representative sample, its shape analysed, quality checks run while the loader is written.
5. Generous types in staging: text stays TEXT.
6. New sources: analyse first, then model on the same architecture (this spec does not cover that; sub-project 4).
7. One heavy disk or database job at a time.

## 4. Components

A new package `src/episteme/ops/`. Each module has one job and a small interface.

| Module | Job | Interface |
|---|---|---|
| `profile.py` | Detect the hardware and compute every runtime value (section 5). | `get_profile() -> Profile` (cached per process); `python -m episteme.ops.profile show [--format=text\|json\|pg]` |
| `lock.py` | The heavy-job lock and the download lock (section 6.1). | `heavy_lock(run_id, *, wait=True, conn=None)` and `download_lock(run_id)` context managers; `python -m episteme.ops.lock acquire\|release\|status` for shell wrappers |
| `retry.py` | Classify an error and retry transient ones (section 6.2). | `is_transient(exc) -> bool`; `with_retries(fn, *, attempts, waits)` |
| `status.py` | Write and read run status files (section 6.6). | `RunStatus(run_dir).update(...)` and `.heartbeat()`; `python -m episteme.ops.status [--stalled]` |
| `gate.py` | The sample run, its report, the thresholds and the pass (section 7). | `run_gate(source, stage) -> GateResult`; `gate_pass_valid(source, stage) -> bool` |

Changes to existing code:

- `config.py` stays the only reader of `os.environ`. It reads every override key named in section 5 and hands them to `profile.py`.
- `parallel.py`, `duckdb_conn.py`, `staging_writer.py`, `postgres_loader.py`, `graph_load.py`, `pmc/download_pmc.py` and the download wrappers take their values from `get_profile()`, not from module constants.
- `scripts/data/run_pipeline.sh` takes the right lock for the stage, writes status, enforces the gate, and applies the failure rules of section 6.
- `scripts/data/db/tune_postgres.sh` reads its values from `profile show --format=pg`.
- A new `scripts/data/run_queue.sh` (section 6.5).

Unchanged: `input_key` identity, success and failure markers, `_lineage`, `_graph_lineage`, the hash-chained audit trail, and the staging schema.

## 5. Profile

### 5.1 Detection

- Physical cores: `psutil.cpu_count(logical=False)`, else `os.cpu_count()`.
- RAM: `psutil.virtual_memory().total`.
- Disk type of each data root (raw, processed, the DuckDB spill folder): Windows `Get-PhysicalDisk` MediaType for the disk that holds the path; Linux `/sys/block/<dev>/queue/rotational`. When the type cannot be found, the profile assumes HDD.
- Detection runs once per process; `profile show` reports the detected facts.
- `psutil` is in `uv.lock` today only as a transitive dependency; PR 1 adds it to `pyproject.toml` as a direct one.

### 5.2 Memory budget

| Share | Rule | 16 GB laptop | 64 GB, 16 cores |
|---|---|---|---|
| OS and other applications | 25% of RAM, at least 3 GB | 4 GB | 16 GB |
| PostgreSQL `shared_buffers` | 15% of RAM, at most 16 GB | 2.4 GB | 9.6 GB |
| PostgreSQL `maintenance_work_mem` | 6% of RAM, at most 2 GB | 1 GB | 2 GB |
| PostgreSQL `work_mem` | 32 MB | 32 MB | 32 MB |
| Pipeline job (workers and DuckDB together) | the rest | about 8.5 GB | about 36 GB |
| DuckDB memory cap | 50% of the pipeline share | about 4 GB | about 18 GB |

### 5.3 Parallelism and sizes

| Value | Rule | Laptop (4 cores, HDD) | 16 cores, SSD |
|---|---|---|---|
| CPU workers (extract, serialize) | physical cores - 1, at least 1, and at most pipeline share / 1.5 GB | 3 | 15 |
| I/O concurrency on the data disk | HDD 2; SSD 4 x cores | 2 | 64 |
| Download threads per host | 16; PMC 48 | 16 / 48 | 16 / 48 |
| PostgreSQL `max_parallel_workers_per_gather` | cores / 2 | 2 | 8 |
| PostgreSQL `effective_io_concurrency` | HDD 2; SSD 200 | 2 | 200 |
| Batch rows (write, fetch, load, graph) | 50,000; 100,000 when RAM >= 48 GB | 50,000 | 100,000 |
| Part caps | 1,000,000 rows or 512 MB of staging data; 2,000,000 rows or 1 GB on an SSD with >= 32 GB RAM | 1M / 512 MB | 2M / 1 GB |

### 5.4 Overrides

Each value has an env key, read by `config.py`; a set key always wins over the rule:
`EPISTEME_EXTRACT_WORKERS`, `EPISTEME_IO_CONCURRENCY`, `EPISTEME_DOWNLOAD_THREADS`, `EPISTEME_PMC_THREADS`, `EPISTEME_DUCKDB_MEMORY_LIMIT`, `EPISTEME_DUCKDB_TEMP_DIR`, `EPISTEME_BATCH_ROWS`, `EPISTEME_PART_ROWS`, `EPISTEME_PART_BYTES`, `EPISTEME_PG_SHARED_BUFFERS`, `EPISTEME_PG_MAINTENANCE_WORK_MEM`, `EPISTEME_PG_WORK_MEM`. An invalid value logs a warning and the rule's value is used. `profile show` prints each value with its source: `detected`, `rule` or `env`.

### 5.5 PostgreSQL

`tune_postgres.sh` applies the values from `profile show --format=pg` with `ALTER SYSTEM` only when the operator runs it. It reports which values need a restart. The profile never changes a running server by itself.

## 6. Execution rules

### 6.1 Locks

- The heavy lock: `load`, `graph`, `enrich`, every `extract` and `serialize`, and migration or purge scripts. There is one holder per machine.
  - A lock file `<data root>/_ops/locks/heavy.lock` holds the PID, run ID, stage and start time.
  - When the stage uses the database, it also takes a PostgreSQL advisory lock with a fixed key.
  - A lock file whose PID no longer exists is stale; the next holder takes it over and logs that.
  - By default a second heavy job waits; `--no-wait` makes it refuse with exit 75.
- The download lock: `download`. A download runs only while no heavy job holds or requests the heavy lock. When a heavy job requests it, the downloader finishes the current file, pauses, and resumes when the heavy lock is released.

### 6.2 Fail fast (extract, serialize, load, graph, enrich)

- A permanent error in a unit (a file or a part) stops the stage at once. `run_pipeline.sh` exits non-zero; the status file and the audit `run_end` row name the unit and the error.
- A transient error is retried 3 times, after 30 s, 60 s and 120 s, then it is treated as permanent. Transient: a lost or refused connection, a timeout, a deadlock, a serialization failure, an OS-level I/O error.
- A stage that is stopped (signal, Ctrl-C, killed parent) cancels its running PostgreSQL statement before it exits, so no orphan backend goes on.

### 6.3 Downloads

- Each file gets the retries that the fetch engine already has. A file that still fails goes into `<run dir>/failed_files.txt`, and the download goes on.
- The stage ends non-zero with that list. A re-run fetches only the failed and missing files.

### 6.4 Resume and bounded parts

- A unit is one input file. When its output passes a part cap (section 5.3), the stage splits it into parts. Each part has its own `source_file` (`<input_key>__part00001`, ...), staging shard, success marker, load transaction and audit row.
- A re-run skips a unit or part whose success marker exists; for a part it reads past the input without writing or loading.
- Part boundaries are stable for an unchanged input. When a source releases a new file, the parts may shift; the loader's id-collision guard moves a row that changed part.
- Small inputs stay one part, so stages with many small files do not change.

### 6.5 Queue runner

`scripts/data/run_queue.sh "<source>:<stage>[,<stage>...]" ...` runs the steps in order. A step is done only when it exits 0 and its log has `pipeline done:`; otherwise the queue stops and writes `STOPPED at <source> <stage>`. The runner takes no lock; each stage takes its own. It never edits or sources a script that is running.

### 6.6 Status

- Each stage writes `<run dir>/status.json` every 30 s and when a unit ends: run ID, source, stage, current unit or part, units done and total, rows done, rate, ETA, last heartbeat (UTC), process IDs, and the PostgreSQL backend PID of the current statement.
- A stage that enters one long statement (a large `DELETE`, a `CREATE INDEX`) marks it as such in the status file.
- `python -m episteme.ops.status` lists the runs. `--stalled` exits 1 when a heartbeat is older than 10 min, or older than 60 min during a marked long statement.
- The scheduled checks and any watcher read these files only.

## 7. Sample gate

- `run_pipeline.sh <source> <stage> --sample` runs the stage on the source's representative slice into `<processed root>/_sample/<source>/`; a sample `load` goes to `episteme_test`. It writes no production data.
- Each source defines its slice in code, for example:
  - `pubchem`: the first 2 parts and 1 part from the middle of the file;
  - `apollo`: one file from each of `train/pretrain` and `train/sft`;
  - `bookshelf`: 20 books spread over the package list;
  - `europepmc_manuscript`: one package.
- The report (`gate_report.json` plus a short Markdown file) gives: rows in and out and the `extract_status` mix; the null rate per column; text length p50, p99 and max; ID uniqueness; orphan bodies (after a sample load); the licence and subset mix; throughput and peak RSS.
- Each source has a rules file with its thresholds, for example `apollo`: 0 empty bodies, 0 duplicate IDs, `dropped` at most 2%.
- A passing report writes `<processed root>/_ops/<source>/gate/<stage>.pass`, with the package version, a hash of the stage module and a hash of the rules file. A change to either hash makes the pass invalid.
- A full run without a valid pass refuses. `--skip-gate --reason "<text>"` overrides it and writes a `gate_override` audit row.

## 8. Testing

All tests are `not pg` unless marked.

- `profile`: the rules with fake hardware (4 cores, 16 GB, HDD; and 16 cores, 64 GB, SSD), each override, an invalid override, an unknown disk type (HDD assumed).
- `lock`: wait and `--no-wait` under contention, the takeover of a stale lock, the advisory lock (`pg`).
- `retry`: the classification of each error type, the number of attempts, no retry for a permanent error.
- `status`: the heartbeat is written, a stalled run is detected, a marked long statement gets the longer limit.
- `gate`: pass, fail, a code change makes the pass invalid, the override is audited (`pg`).
- `run_pipeline.sh` and `run_queue.sh`: a processing stage stops at the first failed unit; a re-run resumes at that unit; a download records a failed file and goes on; the queue stops on a missing `pipeline done:` line. These use fake stages and the existing bash-test pattern.

## 9. Rollout

Separate PRs, in this order, each reviewed (every comment handled) and merged before the next:

1. `profile.py` (with `psutil` as a direct dependency), its `config.py` keys, and the existing stages wired to it; `tune_postgres.sh` on the profile.
2. `retry.py` and fail fast in load, graph, extract and serialize; statement cancel on stop.
3. `lock.py`, `run_queue.sh` and `status.py`, wired into `run_pipeline.sh`.
4. `gate.py` with rules for the next sources: `pubchem`, `apollo`, `bookshelf`, `europepmc_manuscript`.

The docs follow each PR: docs/10 (operator runbook) and the drift log. Sub-project 2 (`pubchem`) starts after PR 4.

## 10. Out of scope

- New sources and their partition design (sub-project 4).
- Runs across more than one machine.
- Rewriting stages that already meet the standard; sub-project 3 lists the gaps.

## 11. Open points

- The exact advisory lock key and the lock file location when the data root is read-only: decided in PR 3.
- Whether a download pause should also apply to the HF CLI downloads, whose process cannot pause mid-file: decided in PR 3 (the default is to let the current file finish).
