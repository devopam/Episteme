# Episteme - Data Sources Operator Runbook

**Status:** Living document (SP5, v2). The single operator runbook for the acquisition and load pipeline.
**Purpose:** Run any wired source, first time or incrementally, against a database you are allowed to touch, and know what "good" looks like.
**Related:** `docs/12-source-inventory.md` (what is wired, per source), `docs/11-gxp-data-integrity.md` (audit trail, DB-mode guard, go-live), `docs/09-extraction-contract.md` (row contract), `docs/08-data-storage-principles.md` (schema, migrations).

Every command in a code fence below is meant to be pasted as-is from the repository root. `tests/docs/test_runbook_doc.py` checks that each `run_pipeline.sh` line is one the dispatcher accepts.

---

## 0. Read this first

### 0.1 What replaced the status board

The old dated "quick status board" is gone. Two documents replace it:

- **What exists and how it is licensed:** `docs/12-source-inventory.md` (static: class, stages wired, licence class and basis, cadence, script).
- **What is on this machine right now:** `scripts/data/source_inventory.sh` (machine-local: newest last sync stamp under the source's raw directory and `episteme.articles` row count per source; section 7).

Audit and integrity rules: `docs/11-gxp-data-integrity.md`. Row contract: `docs/09-extraction-contract.md`. Storage: `docs/08-data-storage-principles.md`.

### 0.2 Ground rules

1. **Agents and CI run against `episteme_test`, never `episteme`.** Prefix every pipeline command with `PGDATABASE=episteme_test`. A real environment variable beats `.env`, so the prefix always wins. Each Bash invocation is a fresh shell in most agent harnesses, so an earlier `export` does not persist: put the prefix on every command.
2. **Confirm the server before any DB step** (section 2.2). This machine can have more than one PostgreSQL installed (a PG 17 client is first on `PATH`); the pipeline database is the native PG19beta3 on `localhost:5433`.
3. **Shell:** Git Bash on Windows (or any bash). Run from the repository root; the default roots are relative paths (`./01_raw`, `./02_processed`, `./03_corpus`) and the `pmc` downloader writes to `<EPISTEME_RAW_ROOT>/pmc/oa_comm`.
4. **Never print `.env`.** It holds credentials. Section 1.3 shows how to check that a key is set without showing its value.
5. **Do not run a bulk download unbounded** unless that is the actual task. Use `--max-files N` (section 4.3) and read section 5 for which sources honour it.

### 0.3 Fast path: smallest real end-to-end run

Source: `openalex`, one works shard, through download, serialize and load (the structured chain; `graph` is not wired for `openalex`, section 4.2). It needs the `aws` CLI (section 1.1) and internet access to the public OpenAlex S3 bucket (`OPENALEX_S3` in `scripts/data/_lib/sources.env`); it does not touch `ftp.ebi.ac.uk`. An alternative literature chain on Europe PMC is at the end of this section.

Prerequisites: section 1.1 tools (including `aws`), `.env` (section 1), a reachable `episteme_test`, and migrations 0002 and 0003 applied (0004 recommended). Run the check in section 2.3 first and expect `t|t|t|t`; if it does not, apply the migrations there before step 3. `EPISTEME_ACTOR` is free text recorded on audit rows; for a test run `episteme_sys_admin` is what the repository's own tests use (the value is only recorded as text on audit rows, so any attributable name works). Because each Bash invocation is a fresh shell (section 0.2 rule 1), the commands below carry `EPISTEME_ACTOR=episteme_sys_admin` as an inline prefix, like `PGDATABASE`; a real environment variable beats `.env`, so the prefix always wins. If the preflight in step 1 prints `EPISTEME_ACTOR=MISSING`, nothing else is needed: the prefix supplies it. Without any value the dispatcher exits 2.

```bash
# 1. preflight: prints only "set" / "MISSING", never a value (expect all set)
bash -c '. scripts/data/_lib/common.sh; load_dotenv 2>/dev/null; for k in EPISTEME_ACTOR PGHOST PGPORT PGUSER PGPASSWORD EPISTEME_SYS_ADMIN_PASSWORD; do if [ -n "${!k:-}" ]; then echo "$k=set"; else echo "$k=MISSING"; fi; done'

# 2. confirm the server and database (expect: episteme_test|19beta3)
PGDATABASE=episteme_test PSQL="/c/Program Files/PostgreSQL/19/bin/psql" bash -c '. scripts/data/_lib/common.sh; load_dotenv 2>/dev/null; PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "${PSQL:-psql}" -X -At -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$PGDATABASE" -c "select current_database(), current_setting(\$\$server_version\$\$)"'

# 3a. download one works_jsonl shard (the listing of the S3 prefix can take minutes)
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh openalex download --max-files 1

# 3b. serialize the shard into the biomedical staging subset
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh openalex serialize --max-files 1

# 3c. load the staged shard into episteme.articles
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh openalex load

# 4. check the audit chain (expect the line: audit chain OK)
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/verify_audit_trail.sh 2>&1 | grep "audit chain"

# 5. see what landed (find the openalex line: rows should now be non-zero)
PGDATABASE=episteme_test bash scripts/data/source_inventory.sh 2>&1 | grep -v -e "couldn't stop" -e "^hint:"
```

Run steps 3b to 5 only if the previous step exited 0; `audit chain OK` does not prove data landed. What to expect, in order (stderr is timestamped `[INFO]` lines). Each `run_pipeline.sh` call is silent for about 20 seconds at startup (the `run_start` audit) before the prerequisites table appears; a full run of steps 3a to 5 took about 3 minutes in the second cold read:

- Every `run_pipeline.sh` run records `run_start` (no output) and prints a prerequisites table (`aria2c`, `aws`, `curl`, `hf`, ... `MISSING` rows are informational; a missing `aws` is not informational for this path).
- Step 3a: `stage: download`, then the `aws` progress line and a `download: s3://... to ...` line for the oldest `updated_date=` shard (a `.gz` of a few MB; it lands under `<raw>/openalex/data/jsonl/works/`; there is no separate listing line, and the step took about a minute), `write_sync_stamp`, then `pipeline done: openalex download`.
- Step 3b: the serializer prints `schema=... source=openalex`, `raw_dir=<path> workers=1`, then `done inputs=1 ok=1 failed=0 rows=<n> records_seen=<m> accepted=<n> rejected_non_biomedical=<k>`. `rows` is only the works tagged Medicine or Biology, so it is smaller than `records_seen`; the count depends on the shard, so do not compare it to a fixed number. Exit 1 with `ERROR: no openalex works_jsonl files under <dir>` means step 3a fetched nothing.
- Step 3c: one `ok <shard> rows=... event=load_commit` line and `done shards=1 failed=0`, then `pipeline done: openalex load`.

If a step fails: the dispatcher prints `stage failed: <name>` and `resume with: ...`; fix the cause and rerun that one command (serialize and load keep per-file and per-shard markers, so finished work is skipped). A failed download wrote nothing to the database. Common causes: `s3_fetch_first_n: awscli required` (install the AWS CLI), `listing failed` (no network path to S3), or a `PGDATABASE` prefix missing (section 3).

On Windows, psycopg pool shutdown noise can appear on stderr from `run_pipeline.sh` (at the start, from the `run_start` audit, and at the end), `verify_audit_trail.sh` and `source_inventory.sh`. Both forms were observed: `hint: you can try to call 'close()' explicitly or to use the pool as context manager` and `couldn't stop thread 'pool-1-...'`. It is harmless when the exit code is 0. They can bury the result line, and a `tail` may show only noise, so grep for the line you want (`audit chain OK`) as in step 4. The exit status after `| grep` is grep's, not the verifier's; the proof is the `audit chain OK` line itself. The inventory table in step 5 lists every wired source, not only `openalex`; rows from earlier work (for example another source with data) are expected, and `n/a` on sources with no data is normal.

**Alternative fast path: Europe PMC preprints.** This runs download, extract, load and graph for three preprints, but it depends on `ftp.ebi.ac.uk` being reachable from your machine (the id list `pprid.txt.gz`). The base URL is the key `EUROPEPMC_PREPRINT_BASE` in `scripts/data/_lib/sources.env`; override it in `.env` if you have a mirror.

```bash
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_preprint all --max-files 3
```

Expected: `stage: download` then `europepmc_preprint: ids=<N> fetched=3 skipped=0 errors=<E>`, `stage: extract` then `done inputs=3 ok=3 failed=0 rows=<n>`, `stage: load` then one `ok <shard> ... event=load_commit` line and `done shards=1 failed=0`, `stage: graph` then `done source_files=...`, and `pipeline done: europepmc_preprint all` with exit code 0. A per-preprint 404, 429 or 503 counts in `errors` and is not fatal. If the id list itself cannot be fetched, the downloader aborts with a raw Python traceback (a `ConnectionError` naming `pprid.txt.gz`), exit code 1, `stage failed: download` and `resume with: ...`; nothing was written to the database. Check with `curl -I` against the base URL plus `/pprid.txt.gz`, retry later, or use the `openalex` path above. The `--dry-run` preview is not a reachability check: with the upstream unreachable it prints a WARN, `would harvest 0 ids`, and exits 0. Treat `0` there as an error, not as nothing to do.

Exit codes of `run_pipeline.sh`: 0 success; 1 a stage failed (the message is `stage failed: <name>` and `resume with: ...`); 130 or 143 a stage was stopped (Ctrl-C, SIGTERM, or the parent watchdog; section 4.7); 2 usage error, missing `EPISTEME_ACTOR`, or `--force` without `--reason`; 3 unknown source, or a source and stage combination that is not wired (section 4.2). Failures that originate inside a stage wrapper (a missing endpoint key such as `OPENALEX_S3` or `COSMOS_API_BASE`, a bad `--max-files`, an unknown mode) exit 2 or 1 when the wrapper is run directly, but through `run_pipeline.sh` they surface as rc 1 (`stage failed: <name>`): every non-zero stage code other than 130 and 143 becomes 1. On Windows, Python stages may also print pool shutdown noise on stderr (`couldn't stop thread 'pool-1-...'` or a `hint: you can try to call 'close()'...` line; observed with `run_pipeline.sh`, `verify_audit_trail.sh` and `source_inventory.sh`, see section 0.3); they were not seen to affect the exit code.

If step 2 does not print `episteme_test|19beta3`, stop: you are talking to a different server or database.

---

## 1. Environment and `.env`

Configuration is read from three places. Shell scripts load them in this order, last wins: `scripts/data/_lib/sources.env` (committed endpoint defaults) then `./.env` then the real environment. `src/episteme/config.py` is the only Python reader and ranks them real environment, then `.env`, then `sources.env`. In both, a real environment variable **with a non-empty value** beats `.env`.

### 1.1 Prerequisites

| Need | For | Check |
|---|---|---|
| Git Bash (or bash) | every `scripts/data/*.sh` | `bash --version` |
| Python venv `.venv` with `pip install -e ".[data]"` | all Python stages; scripts look for `.venv/Scripts/python.exe`, then `.venv/bin/python`, then `python`. Override with `PYTHON=/path/to/python`. | `.venv/Scripts/python.exe -c "import episteme"` |
| PostgreSQL 19 client (`psql`) | section 2 and the migration commands. Scripts prefer `/c/Program Files/PostgreSQL/19/bin/psql`; override with `PSQL=...`. | `"/c/Program Files/PostgreSQL/19/bin/psql" --version` |
| `curl` | every listing-based download, size checks | `curl --version` |
| `aria2c` | fast segmented downloads; without it downloads fall back to sequential `curl` (slow, warned) | `aria2c --version` |
| `aws` CLI (or `s5cmd`) | `openalex` only | `aws --version` |
| Hugging Face CLI (`hf`, or the older `huggingface-cli`) | `apollo`, `guidelines`, `hf_corpus` only | `hf --version` |

`scripts/data/_lib/check_prereqs.sh` prints this tool table (it never fails) at the start of every `run_pipeline.sh` run.

`hf_fetch` (`scripts/data/_lib/hf_download.sh`) downloads the pinned repo type only. It tries up to `EPISTEME_HF_TRIES` times (default 3, waiting `EPISTEME_HF_RETRY_DELAY` × attempt seconds, default 30); the CLI resumes the files it already has. It sets `HF_HUB_DOWNLOAD_TIMEOUT` and `HF_HUB_ETAG_TIMEOUT` to 60 s unless they are set. When the CLI says the repo "cannot be accessed", it exits 0 and returns the old local folder, so the step used to report success with nothing downloaded; `hf_fetch` treats that as a failed attempt. Behind a 401, 403 or 404 (a gated repo without `HF_TOKEN` or with its terms not accepted, a private repo, a wrong repo or type) it fails at once; behind a timeout, a lost connection or a hub 5xx it tries again.

### 1.2 Keys (names only)

Values are never shown in this document. Put them in `.env` (gitignored) or the real environment.

| Key | Meaning |
|---|---|
| `EPISTEME_ACTOR` | **Required** by `run_pipeline.sh` and every stage wrapper (the dispatcher and the wrappers exit 2 if unset). Identity recorded on every audit row (free text; for a test run `episteme_sys_admin` is acceptable). `source_inventory.sh` does not need it; `db/*.sh` do not use it. |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` | Connection used by the Python stages (role `episteme_app` at runtime). `PGDATABASE` is what the guard checks (section 3). |
| `EPISTEME_DB_PASSWORD` | Password `init_database.sh` sets for `episteme_app`. |
| `EPISTEME_SYS_ADMIN_PASSWORD` | Password of `episteme_sys_admin`, the DDL role used by `db/*.sh` and the migration commands below. |
| `POSTGRES_SUPERUSER`, `POSTGRES_SUPERUSER_PASSWORD` | Bootstrap role for `install_extensions.sh` and `init_database.sh`, for `tune_postgres.sh apply`, and for the MCPg admin server (operator only; see section 2). |
| `MCPG_ALTER_SYSTEM_ALLOWLIST` | Optional. Comma-separated settings the MCPg admin server may change (default: the section 2.4 settings plus `max_parallel_maintenance_workers`). `scripts/mcpg-episteme.sh` sets `MCPG_ALLOW_ALTER_SYSTEM` itself: on for `--admin`, off otherwise. |
| `EPISTEME_DATA_ROOT` | Base of the three roots below (default `.`). |
| `EPISTEME_RAW_ROOT`, `EPISTEME_PROCESSED_ROOT`, `EPISTEME_CORPUS_ROOT` | Default `<data root>/01_raw`, `<data root>/02_processed`, `<data root>/03_corpus`. |
| `EPISTEME_LOCK_DIR` | Folder of the heavy-job and download locks (default `<data root>/_ops/locks`). |
| `EPISTEME_DB_MODE`, `EPISTEME_DB_TARGET`, `PGDATABASE_SECONDARY`, `EPISTEME_PRODUCTION_DATABASE` | The database-mode guard (section 3). |
| `NCBI_API_KEY` | Optional. Raises NCBI rate limits for the PMC downloader. |
| `EPISTEME_DOWNLOAD_THREADS` | Parallel aria2c jobs. Unset: 4 in the aria2c wrappers. The profile computes `download_threads` (16), but no wrapper reads it yet. |
| `EPISTEME_PMC_THREADS` | Threads of the `pmc` downloader (`--threads`). Unset: the profile's rule (48). |
| `EPISTEME_DUCKDB_MEMORY_LIMIT` | Memory cap for the serializers' DuckDB connections (unset: the profile's rule, 50% of the pipeline share, about 4 GB on a 16 GB machine; spills to `EPISTEME_DUCKDB_TEMP_DIR`). |
| `EPISTEME_DUCKDB_TEMP_DIR` | Spill root for those connections (default `<processed root>/_ops/_duckdb_tmp`). Set it to a fast local disk when the processed root is on a slow disk: the `pubchem` join spilled 31 GB at random-I/O speed to the USB HDD and slowed every other job on it (2026-10-18). |
| `EPISTEME_EXTRACT_WORKERS` | Worker processes per extract stage and the `openalex` serialize (unset: the profile's rule, the physical cores minus one, limited by memory; 3 on the 4-core laptop). `--workers N` on a module overrides it. |
| `EPISTEME_IO_CONCURRENCY` | Parallel reads or writes on the data disk. Unset: the profile's rule (2 on an HDD or an unknown disk kind; 4 x cores on an SSD). The profile reads it, but no stage uses it yet. |
| `EPISTEME_BATCH_ROWS` | Rows per write, fetch, load and graph batch. Unset: the profile's rule (50,000; 100,000 when RAM is 48 GB or more). A staging write batch also ends at 64 MB of string data (`staging_writer.WRITE_BATCH_BYTES`), whatever its row count. |
| `EPISTEME_PART_ROWS`, `EPISTEME_PART_BYTES` | Row and byte caps of one part. The profile reads them. No stage writes parts yet. |
| `EPISTEME_PG_SHARED_BUFFERS`, `EPISTEME_PG_MAINTENANCE_WORK_MEM`, `EPISTEME_PG_WORK_MEM` | Values that `tune_postgres.sh` applies. Unset: the profile's rule (section 2.5). |
| `EPISTEME_SAMPLE_LIMIT` | Default for the extract/serialize `--max-files` when the flag is absent (default 0 = all files). |
| `EPISTEME_FETCH_BATCH` | URLs per batch in `http_fetch` (default 64). A download pauses between batches (section 4.8). A value that is not a positive number gives a WARNING and 64. |
| `EPISTEME_RUN_DIR` | Set by `run_pipeline.sh`: the folder of the run (`<processed root>/_ops/runs/<run id>`). It holds `status.json` and `failed_files.txt`. Leave it blank. |
| `EPISTEME_STOP_FILE` | Set by `run_pipeline.sh`: the stop-request file of the run (`<run folder>/stop.request`; without a run folder, a file in the temp folder). The runner writes it on Ctrl-C or SIGTERM, and the stage stops (section 4.7). Leave it blank. |
| `EPISTEME_RUN_ID` | Set by `run_pipeline.sh` for each invocation so all stages share one run id. Leave it blank. |
| `EPISTEME_INIT_FORCE` | `1` makes `init_database.sh` DROP and recreate the `episteme` schema. Destructive. |
| `OM_HOST`, `OM_JWT` | Optional OpenMetadata ingest; blank means skip. |
| `PYTHON`, `PSQL` | Interpreter and client overrides for the scripts. |
| Endpoint keys (`PUBMED_FTP_BASE`, `EUROPEPMC_BASE`, `EUROPEPMC_PREPRINT_BASE`, `CHEMBL_BASE`, `UNIPROT_MIRRORS`, `OPENALEX_S3`, `COSMOS_API_BASE`, ...) | Defaults live in `scripts/data/_lib/sources.env`. Override in `.env` or the environment; never edit a wrapper. |

Layout under the roots: raw downloads in `<raw>/<source>/` (nested for the Europe PMC feeds: `<raw>/europepmc/<preprints|manuscripts|id_mappings|lite_metadata|abstracts>/`), staging shards in `<processed>/staging/<source>/`, per-file markers under `<processed>/_ops/<source>/`, the audit JSONL mirror in `<processed>/_ops/_audit/`, corpus shards under `<corpus>/`. `last_sync_utc.txt` under a source's raw directory (possibly in a nested folder, e.g. `<raw>/openalex/data/jsonl/works/`) is the stamp `source_inventory.sh` reads; it reports the newest one found under the source's raw directory.

### 1.3 The `.env` file rules

- **Plain `KEY=value` lines only.** The shell loader (`load_dotenv` in `scripts/data/_lib/common.sh`) does **not** strip inline comments, so `KEY=value   # note` loads the value `value   # note`. Put comments on their own lines. It strips one matching pair of surrounding quotes, tolerates CRLF, and does not understand an `export ` prefix.
- `.env.example` currently carries inline comments on several lines (for example `EPISTEME_DATA_ROOT=.` followed by a comment). Strip them when you copy it to `.env`; the Python side tolerates them but the shell side does not.
- Values that contain spaces must be quoted.
- Check a key without printing it: the preflight command in section 0.3. `.env` is gitignored; never paste or commit it.

### 1.4 Windows notes

- `psql` on `PATH` may be an older version; pass `PSQL="/c/Program Files/PostgreSQL/19/bin/psql"` as shown.
- The field-shape `--report` mode (section 6.3) crashes with a `cp1252` encoding error on Windows consoles when a record contains non-ASCII text. Run it with `PYTHONIOENCODING=utf-8`.

---

## 2. Database setup

### 2.1 Roles and databases

Native PostgreSQL 19beta3, `localhost:5433`, databases `episteme` (primary, production name) and `episteme_test` (secondary; agents and CI). Roles: `postgres` (bootstrap only), `episteme_sys_admin` (DDL, migrations, developer), `episteme_app` (pipeline runtime; INSERT and SELECT only on `_audit`).

Bootstrapping is an operator task and is normally already done. **Agents do not run these**; `init_database.sh` creates and touches *both* databases including production, and the guard does not cover raw `psql` scripts (docs/11 section 7):

```bash
bash scripts/data/db/install_extensions.sh
bash scripts/data/db/init_database.sh
```

- `install_extensions.sh` tries `CREATE EXTENSION vector` and `pg_search` in both databases. On the local PG19beta3 build the extension files are absent: it prints the server error, defers to migration 0001, and still exits 0.
- `init_database.sh` is idempotent: roles and databases only if absent, `extensions.sql`, then `schema.sql` per database. An existing `episteme` schema is left alone unless `EPISTEME_INIT_FORCE=1`, which drops and recreates it (data loss). It needs `PGHOST`, `PGPORT`, `EPISTEME_SYS_ADMIN_PASSWORD` and `EPISTEME_DB_PASSWORD` and never echoes a password.

### 2.2 Confirm the server before any DB step (mandatory for agents)

```bash
PGDATABASE=episteme_test PSQL="/c/Program Files/PostgreSQL/19/bin/psql" bash -c '. scripts/data/_lib/common.sh; load_dotenv 2>/dev/null; PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "${PSQL:-psql}" -X -At -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$PGDATABASE" -c "select current_database(), current_setting(\$\$server_version\$\$)"'
```

Expected on the local build: `episteme_test|19beta3`. Any other database name or version means you are on the wrong server or the wrong database: stop and fix `PGPORT`/`PGDATABASE` before continuing. (The check uses `select current_setting('server_version')` written with `$$` quoting so it survives the shell.)

Since SP6, running the automated test suite's `-m pg` tests (e.g. `PGDATABASE=episteme_test .venv/Scripts/python.exe -m pytest -m pg`) needs `TEST_PG_DSN`/`EPISTEME_SYS_ADMIN_PASSWORD` present as real shell environment variables *before pytest starts* — source `.env` into the shell first with `set -a; . ./.env; set +a` (never `cat`/print it), or `tests/data/conftest.py`'s `pg_conn` fixture finds `TEST_PG_DSN` unset and every pg-marked test silently skips rather than failing loudly.

### 2.3 Migrations: 0001 blocked, 0002-0009 applied by hand

Migrations live in `src/episteme/data/db/migrations/`. `scripts/data/db/migrate_database.sh [target_db]` applies them in order and records each in `episteme._migrations`, but it stops at the first failure:

| Migration | Adds | Status on the local build |
|---|---|---|
| `0001_add_chunk_vector_columns.sql` | `chunks.embedding` (pgvector) and `chunks.chunk_tsv` | **Cannot apply:** `vector` and `pg_search` are absent on PG19beta3. Needed only for Phase 1 retrieval. |
| `0002_container_and_book_parts.sql` | `articles.container_id` and `book_meta`, the `bookshelf` partitions, `article_parts` | Required before **any** load (the loader writes those columns) and before `graph`. |
| `0003_mesh_hierarchy.sql` | `episteme.mesh_hierarchy` | Required before `mesh` graph. |
| `0004_articles_load_indexes.sql` | `articles (source, source_file)` and `articles (id)` indexes (per partition) | Recommended before any large load: without them each per-file delete scans the whole `articles_default` partition. Apply it with **no load running**: `CREATE INDEX` on the partitioned parent blocks writes to every partition while it builds (`CONCURRENTLY` is not possible on a partitioned parent). |
| `0005_bulk_index_state.sql` | `episteme._bulk_index_state`, the bookkeeping of bulk-load mode (section 4.6) | Required before `load --bulk`. |
| `0006_pubmed_graph.sql` | Graph tables `article_comments`, `article_chemicals`, `article_keywords`, `article_authors`, `article_facets`, `mesh_tree`, `mesh_facet_rule` (seeded, rule_version 1) and `_graph_lineage`; new columns on `article_mesh` and `article_cites`; the `bulk_indexes` group `pubmed_graph` covers their secondary indexes | Required before `pubmed graph` and `mesh graph` (which fills `mesh_tree`). Needs schema 1.6 shards (docs/09 section 4.5). |
| `0007_pubmed_partition.sql` | `pubmed` LIST partitions: `articles_pubmed` (year ladder: 13 ranges plus a `DEFAULT` for a NULL or out-of-range year) and `article_body_pubmed` | A full `pubmed` reload can `TRUNCATE` these (an audited operator step for now; the loader still replaces shard by shard, fast while the partitions are empty) instead of deleting rows scattered through the default partition. **Precondition:** no `pubmed` row in `articles_default` / `article_body_default` (the `CREATE TABLE ... PARTITION OF` scans the default partition and fails otherwise), so on a loaded database purge `pubmed` first (one audited `DELETE ... WHERE source = 'pubmed'`). Apply with no load running. |
| `0008_pmc_bookshelf_default_partition.sql` | `DEFAULT` year partitions `articles_pmc_default` and `articles_bookshelf_default` | A `pmc` or `bookshelf` row with a NULL or out-of-range year lands there instead of failing its batch. No precondition and no scan of the year partitions; apply before the first `pmc` or `bookshelf` load. |
| `0009_apollo_pubchem_partitions.sql` | `apollo` and `pubchem` LIST partitions: `articles_apollo`, `articles_pubchem`, `article_body_apollo`, `article_body_pubchem` (no year split) | Keeps the loader's per-shard id deletes off the ~30-million-row default partition (an apollo shard spent an hour or more there). **Precondition:** no `apollo` or `pubchem` row in `articles_default` / `article_body_default` (the `CREATE TABLE ... PARTITION OF` scans the default partition and fails otherwise), so on a loaded database purge first (audited `DELETE ... WHERE source = ...`, and remove the purged shards' load markers `<processed root>/_ops/<source>/load_success/<shard>.ok` and their `episteme._lineage` rows, `WHERE source = '<source>'`, so the shards load again). Apply with no load running. |

Because `migrate_database.sh` dies at 0001 and never reaches 0002 to 0009, apply those eight directly (0004 with no load running, see the table). All are idempotent (safe to re-run); applied this way they are **not tracked** in `episteme._migrations` (see the header of each file and docs/08 section 9). Use `episteme_sys_admin`, never `episteme_app`:

```bash
PGDATABASE=episteme_test PSQL="/c/Program Files/PostgreSQL/19/bin/psql" bash -c '. scripts/data/_lib/common.sh; load_dotenv 2>/dev/null; for f in 0002_container_and_book_parts 0003_mesh_hierarchy 0004_articles_load_indexes 0005_bulk_index_state 0006_pubmed_graph 0007_pubmed_partition 0008_pmc_bookshelf_default_partition 0009_apollo_pubchem_partitions; do PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "${PSQL:-psql}" -X -v ON_ERROR_STOP=1 -q -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$PGDATABASE" -f "src/episteme/data/db/migrations/$f.sql" || exit 1; done; echo migrations-applied'
```

Expect `migrations-applied` and exit 0. Check the result (expect `t|t|t|t|t`; the fourth column is 0004's `articles_id_idx` and the fifth is 0005's table):

```bash
PGDATABASE=episteme_test PSQL="/c/Program Files/PostgreSQL/19/bin/psql" bash -c '. scripts/data/_lib/common.sh; load_dotenv 2>/dev/null; PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "${PSQL:-psql}" -X -At -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$PGDATABASE" -c "select to_regclass(\$\$episteme.article_parts\$\$) is not null, to_regclass(\$\$episteme.mesh_hierarchy\$\$) is not null, exists(select 1 from information_schema.columns where table_schema=\$\$episteme\$\$ and table_name=\$\$articles\$\$ and column_name=\$\$container_id\$\$), to_regclass(\$\$episteme.articles_id_idx\$\$) is not null, to_regclass(\$\$episteme._bulk_index_state\$\$) is not null"'
```

**Migration 0006 through MCPg.** Where the database is reached through the MCPg MCP server, apply 0006's DDL with `run_ddl` (it is idempotent). `run_ddl` refuses GRANT and REVOKE, so the operator then runs the file's single `REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON episteme.mesh_facet_rule FROM episteme_app;` line once as `episteme_sys_admin` (the psql loop above does this itself). The pipeline writes as `episteme_app`, so every other new table keeps the standard DML grants; only `mesh_facet_rule` is read-only to it, so facet rules change only through `episteme_sys_admin`. Check: `to_regclass('episteme.article_authors')` and `to_regclass('episteme._graph_lineage')` are not null, and `episteme.mesh_facet_rule` holds 31 rows.

On a server where pgvector is installed, `bash scripts/data/db/migrate_database.sh episteme_test` applies 0001 through 0009 and tracks them. Do not run it on the local build.

### 2.4 Server settings for bulk loads

PostgreSQL's defaults suit a small shared server, not a single-user bulk load. On the local build (4 cores, 16 GB RAM, data directory on a 5,400 rpm USB HDD) these settings are recommended before a full load. The five memory and parallelism values come from the runtime profile (section 2.5); the table shows the values for this laptop. They are cluster-wide, so they apply to `episteme` and `episteme_test` alike.

| Setting | Default | Recommended | Restart | Why |
|---|---|---|---|---|
| `shared_buffers` | 128MB | profile: 15% of RAM, about 2.4GB | yes | keeps the hot index pages of `articles_default` in memory during loads |
| `max_wal_size` | 1GB | 16GB | no | fewer forced checkpoints during a load |
| `min_wal_size` | 80MB | 2GB | no | keeps recycled WAL segments instead of recreating them |
| `checkpoint_timeout` | 5min | 30min | no | fewer checkpoints, so fewer full-page images in the WAL |
| `wal_compression` | off | lz4 | no | compresses those full-page images (needs a build with lz4; the local PG19 has it) |
| `maintenance_work_mem` | 64MB | profile: 6% of RAM, about 1GB | no | faster `CREATE INDEX` and index rebuilds |
| `work_mem` | 4MB | profile: 32MB | no | per-sort memory; the profile keeps it small |
| `effective_io_concurrency` | 16 | profile: 2 on an HDD or an unknown disk kind; 200 on an SSD | no | prefetch depth that suits the disk |
| `max_parallel_workers_per_gather` | 2 | profile: cores / 2 | no | parallel query workers |
| `effective_cache_size` | 4GB | 8GB | no | planner hint only; allocates nothing |

`fsync`, `full_page_writes` and data checksums stay on (GxP data integrity, docs/11). Memory budget on 16 GB: the profile gives about 2.4 GB to shared buffers, about 1 GB to `maintenance_work_mem`, up to about 4 GB to DuckDB during a serialize, and the rest of the pipeline share to the Python workers. At least 25% of RAM stays free for the operating system and its cache. `max_wal_size`, `min_wal_size`, `checkpoint_timeout`, `wal_compression` and `effective_cache_size` stay fixed.

Check and apply with `scripts/data/db/tune_postgres.sh`. The script reads the five profile values from `python -m episteme.ops.profile show --format pg`; an `EPISTEME_PG_*` key overrides a value. `check` reads `pg_settings` as `episteme_sys_admin` and exits 1 if any setting differs, if a restart is pending, or if an `ALTER ROLE`/`ALTER DATABASE ... SET` override of one of these settings exists (it would beat the server value for, say, `episteme_app` sessions). `apply` needs the superuser, because `episteme_sys_admin` has no `ALTER SYSTEM` privilege (by design): it uses `POSTGRES_SUPERUSER` (default `postgres`) and `POSTGRES_SUPERUSER_PASSWORD` from `.env`, like `init_database.sh`, validates every name and value first (nothing is written if one is invalid), sets each value, reloads, and confirms each one is active or waiting for a restart. Every connection uses `PGCONNECT_TIMEOUT` (default 10 seconds). The everyday MCPg server cannot do this: it connects as `episteme_sys_admin`, and its `run_ddl` tool accepts only structural DDL and rejects `ALTER SYSTEM`.

**MCPg admin server (optional).** MCPg 0.8 adds `alter_system`, `alter_system_reset_all` and `reload_config` tools, plus a read-only `inspect_guc` that shows a setting's live value and whether it needs a reload or a restart. `scripts/mcpg-episteme.sh episteme --admin` starts MCPg as `POSTGRES_SUPERUSER` with those tools on, `MCPG_ACCESS_MODE=unrestricted`, no secondary database, and an allowlist (`MCPG_ALTER_SYSTEM_ALLOWLIST`). Without `--admin` the launcher always turns them off. Each change takes two calls: the first returns a warning and a confirmation token, and the second applies it with `confirm=true` and that token. `ALTER SYSTEM` is not transactional; a `postmaster` setting such as `shared_buffers` still needs a service restart. `alter_system_reset_all` clears every `ALTER SYSTEM` override, including the section 2.4 tuning; MCPg refuses it while any override is outside the allowlist. Do not change settings through MCPg while `tune_postgres.sh apply` runs: both write `postgresql.auto.conf`, and the last write wins.

Because every MCPg tool on the admin server runs as the superuser, keep it disabled and enable it in `/mcp` only while changing settings. Example entry (in `.mcp.json`, or in the user config with Git Bash's absolute path on Windows):

```json
"mcpg-admin": { "command": "bash", "args": ["scripts/mcpg-episteme.sh", "episteme", "--admin"], "env": {} }
```

Disable it when you add it. For a `.mcp.json` entry, list it in `disabledMcpjsonServers` in `.claude/settings.local.json`, for example `{"disabledMcpjsonServers": ["mcpg-admin"]}`. For a user-config entry, toggle it off in `/mcp` straight after adding it; Claude Code remembers that per project.

```bash
bash scripts/data/db/tune_postgres.sh check
bash scripts/data/db/tune_postgres.sh apply     # then restart the postgresql-x64-19 service
```

**Write-ahead log on a faster disk (recommended).** With the data directory on one slow HDD, the sequential WAL writes compete with the random table and index writes. Moving `pg_wal` to the internal NVMe SSD separates them. It is an operator task, run elevated, with the service stopped:

1. Stop `postgresql-x64-19` and confirm `postmaster.pid` is gone from the data directory.
2. Copy `<data>\pg_wal` to the new location (for example `C:\pgwal\episteme19\pg_wal`) and compare file counts and byte totals.
3. Grant the service account (`NT AUTHORITY\NetworkService`) full control of the new directory.
4. Rename `<data>\pg_wal` to `pg_wal.old` and create a junction in its place: `New-Item -ItemType Junction -Path <data>\pg_wal -Target C:\pgwal\episteme19\pg_wal`. This is the layout `initdb --waldir` creates on Windows.
5. Start the service, check that the server is not in recovery, run a `CHECKPOINT`, verify the audit chain, then delete `pg_wal.old`.

Cost: the database then depends on both drives; back-ups must cover both. `max_wal_size` is a soft target, not a cap: a load that writes WAL faster than checkpoints can recycle it, or WAL held back by archiving or a replication slot (neither is used on the local build), can grow `pg_wal` past it. Give the WAL disk generous headroom, at least twice `max_wal_size` (32 GB at 16GB; the local SSD has hundreds of GB free), and watch its size during the first large load. A full WAL disk stops all writes.

### 2.5 Runtime profile

The profile sets the memory and parallelism values of the pipeline from the hardware. It reads the physical cores, the RAM and the disk kind of the raw root, the processed root and the DuckDB spill folder. When it cannot find the disk kind, it uses the HDD rules. Detection runs once per process.

Show the profile:

```bash
.venv/Scripts/python -m episteme.ops.profile show                  # text (default)
.venv/Scripts/python -m episteme.ops.profile show --format json
.venv/Scripts/python -m episteme.ops.profile show --format pg      # the values for tune_postgres.sh
```

The first line of the text output shows the hardware and ends with `detected`. Each value line ends with its source: `rule` or `env`.

`effective_io_concurrency` follows the disk kind of the processed root, not of the PostgreSQL data directory. Run `tune_postgres.sh` from a checkout whose processed root is on the same kind of disk as the server's data (on production, both are on D:).

**An env key wins.** A set key always replaces the rule's value. An invalid value logs a warning, and the rule's value is used. The keys are in section 1.2: `EPISTEME_EXTRACT_WORKERS`, `EPISTEME_IO_CONCURRENCY`, `EPISTEME_DOWNLOAD_THREADS`, `EPISTEME_PMC_THREADS`, `EPISTEME_DUCKDB_MEMORY_LIMIT`, `EPISTEME_DUCKDB_TEMP_DIR`, `EPISTEME_BATCH_ROWS`, `EPISTEME_PART_ROWS`, `EPISTEME_PART_BYTES`, `EPISTEME_PG_SHARED_BUFFERS`, `EPISTEME_PG_MAINTENANCE_WORK_MEM` and `EPISTEME_PG_WORK_MEM`. A `--workers N` flag on a module wins over the key.

**Memory budget**

| Share | Rule | 16 GB laptop | 64 GB, 16 cores |
|---|---|---|---|
| OS and other applications | 25% of RAM, at least 3 GB | 4 GB | 16 GB |
| PostgreSQL `shared_buffers` | 15% of RAM, at most 16 GB | 2.4 GB | 9.6 GB |
| PostgreSQL `maintenance_work_mem` | 6% of RAM, at most 2 GB | 1 GB | 2 GB |
| PostgreSQL `work_mem` | 32 MB | 32 MB | 32 MB |
| Pipeline job (workers and DuckDB together) | the rest | about 8.5 GB | about 36 GB |
| DuckDB memory cap | 50% of the pipeline share | about 4 GB | about 18 GB |

**Parallelism and sizes**

| Value | Rule | Laptop (4 cores, HDD) | 16 cores, SSD |
|---|---|---|---|
| CPU workers (extract, serialize) | physical cores - 1, at least 1, and at most pipeline share / 1.5 GB | 3 | 15 |
| I/O concurrency on the data disk | HDD 2; SSD 4 x cores | 2 | 64 |
| Download threads per host | 16; PMC 48 | 16 / 48 | 16 / 48 |
| PostgreSQL `max_parallel_workers_per_gather` | cores / 2 | 2 | 8 |
| PostgreSQL `effective_io_concurrency` | HDD 2; SSD 200 | 2 | 200 |
| Batch rows (write, fetch, load, graph) | 50,000; 100,000 when RAM >= 48 GB | 50,000 | 100,000 |
| Part caps | 1,000,000 rows or 512 MB of staging data; 2,000,000 rows or 1 GB on an SSD with >= 32 GB RAM | 1M / 512 MB | 2M / 1 GB |

**Example (this laptop, from a worktree without `.env`).** In that worktree the data folders were on the internal SSD, so the profile applied the SSD rules (`io_concurrency` 16 and `pg_effective_io_concurrency` 200):

```
hardware: 4 cores, 15 GB RAM, disks {'raw': 'ssd', 'processed': 'ssd'}
os_reserve_bytes                           4262667264  rule
pipeline_bytes                             9173806859  rule
cpu_workers                                         3  rule
io_concurrency                                     16  rule
download_threads                                   16  rule
pmc_threads                                        48  rule
duckdb_memory_bytes                        4586903429  rule
batch_rows                                      50000  rule
part_rows                                     1000000  rule
part_bytes                                  536870912  rule
pg_shared_buffers_bytes                    2557600358  rule
pg_maintenance_work_mem_bytes              1023040143  rule
pg_work_mem_bytes                            33554432  rule
pg_max_parallel_workers_per_gather                  2  rule
pg_effective_io_concurrency                       200  rule
```

On the production layout, the data is on a USB HDD that Windows reports as `Unspecified`. There the disk kind is `unknown`, and the HDD rules apply. `io_concurrency` is then 2, and `effective_io_concurrency` is 2. Run `profile show` on the machine to see the real values.

The `--format pg` output gives `tune_postgres.sh` its five values: `shared_buffers`, `maintenance_work_mem`, `work_mem`, `effective_io_concurrency` and `max_parallel_workers_per_gather`. The profile never changes a running server. Only `tune_postgres.sh apply` does (section 2.4).

---

## 3. The database-mode guard

Full statement: `docs/11-gxp-data-integrity.md` section 7. Operator summary:

| Setting | Values | Effect |
|---|---|---|
| `EPISTEME_DB_MODE` | `read-only`, `restricted`, `unrestricted` | `restricted` (the default when unset) refuses to open a connection when the target database name equals `EPISTEME_PRODUCTION_DATABASE` (default `episteme`). `read-only` opens connections with `default_transaction_read_only=on`. `unrestricted` never refuses. Any other value is a config error. |
| `EPISTEME_DB_TARGET` | `primary` (default), `secondary` | `primary` uses `PGDATABASE`; `secondary` uses `PGDATABASE_SECONDARY` (must be set, else a config error). |
| `PGDATABASE_SECONDARY` | database name | The secondary target; `episteme_test` here. |
| `EPISTEME_PRODUCTION_DATABASE` | database name | The name the guard protects. Default `episteme`. |

Rules of use:

- **Agents and CI target `episteme_test`:** `PGDATABASE=episteme_test` on every command (or `EPISTEME_DB_TARGET=secondary`). With the shipped `.env` (`PGDATABASE=episteme`, mode `restricted`), a command without the prefix is refused with `Refusing to open a connection to production database 'episteme'`; nothing is written. Symptom in `run_pipeline.sh`: `WARN run_start audit failed; proceeding unaudited (... refused by the DB-mode guard ...)`, and DB-writing stages fail. Python stages that audit best-effort (extract, serialize) keep going and fall back to an unchained mirror line, so a missing prefix can also look like a quiet success; always check the `PGDATABASE` prefix first.
- The guard compares the **database name only**. It does not check the server, and it does not cover raw `psql` and `scripts/data/db/*.sh`. It is a safety rail, not an access control.
- Do not use `EPISTEME_DB_MODE=unrestricted` from an agent. The `.env` template ships `restricted`.
- **Go-live:** production runs set `EPISTEME_DB_MODE=restricted` in the environment (a development convenience of `unrestricted` must not be carried over) and follow the checklist in docs/11 section 8: verify the audit chain, confirm audit partitions exist for the current and coming months (only `_audit_202609`, `_audit_202610` and a default partition ship), protect the mirror directory, confirm server identity by hand. A deliberate production run uses a one-command override, `EPISTEME_DB_MODE=unrestricted`, prefixed on that command only.

---

## 4. Pipeline model

### 4.1 The dispatcher

```text
bash scripts/data/run_pipeline.sh <source> <stage> [--dry-run] [--force] [--reason REASON] [--max-files N] [--no-wait] [--skip-unit NAME] [--sample] [--skip-gate --reason REASON]
bash scripts/data/run_pipeline.sh corpus materialize
```

Stages: `all`, `download`, `extract`, `load`, `graph`, `materialize`, `enrich`, `serialize`. Sources are the keys of the `WRAPPER` table in `scripts/data/run_pipeline.sh` (all listed in `docs/12-source-inventory.md`), plus the alias `corpus` (materialize only).

Order of checks: usage (no source or stage: exit 2), then `EPISTEME_ACTOR` (exit 2), then argument validation (`--force` without `--reason`: exit 2; `--skip-unit` without `--reason`: exit 2; `--skip-unit` with any stage other than `extract`, `serialize` or `load` (for example `all` or `graph`), or with the `load` stage of `europepmc_id_mappings` or `cdisc_ct`: exit 2; `--skip-gate` without `--reason`, or with `--sample`: exit 2; `--sample` with any stage other than `extract`, `serialize` or `load`, or with `--bulk`, `--force`, `--dry-run`, `--max-files` or `--skip-unit`: exit 2; `--sample` without `PGDATABASE_SECONDARY`: exit 2), then an unknown source (exit 3; these dispatcher checks are the only place rc 2 and 3 come from, except the `--bulk` index drop and rebuild (section 4.7); a wrapper's own failures, including a missing endpoint key, surface as rc 1), an unrecognised stage word (usage, exit 2) and an unwired combination (exit 3; a `--dry-run` of a gated stage stops here, before any gate check), then the sample gate (section 4.9: a full run of a gated stage without a valid pass exits 2; a `--sample` run checks its rules, its sample staging and the migrations of the secondary database), then, for a single stage, the stage lock (section 4.8: a busy lock under `--no-wait` exits 75), then the audit bracket and the stage. All validation happens before any audit row, so a rejected command leaves no dangling `run_start`. A refusal by the lock also leaves no `run_start`.

Tokens the dispatcher does not recognise are not dropped: they are forwarded to the download wrapper as the MODE or repo argument (for example `parquet` for `openalex`, `all` for `chembl`, a repo id for `hf_corpus`) and to flags such as `--include-current` or `--since`. Only the download wrapper sees them.

### 4.2 Stage matrix

| Source class | download | extract | serialize | load | graph | enrich | materialize | all |
|---|---|---|---|---|---|---|---|---|
| `pmc` | yes | yes | no (exit 3) | yes | yes | yes | yes | download, extract, load, graph, materialize, enrich |
| Literature: `pubmed`, `apollo`, `europepmc_preprint`, `europepmc_manuscript`, `guidelines`, `bookshelf` | yes | yes | no | yes | yes | no | no | download, extract, load, graph |
| Structured: `chembl`, `uniprot`, `pubchem`, `clinvar`, `reactome`, `mesh`, `ontologies`, `openalex`, `cdisc_ct` | yes | no | yes | yes | `mesh` only | no | no | download, serialize, load (plus graph for `mesh`) |
| `europepmc_id_mappings` | yes | no | no | yes (to `episteme.id_map`) | no | no | no | download only |
| `europepmc_lite` | yes | no | no | no | no | yes | no | download only |
| `europepmc_abstracts`, `hf_corpus`, `dailymed`, `openfda`, `aact`, `cdisc_bc`, `cdisc_usdm` | yes | no | no | no | no | no | no | download only |
| `corpus` (alias) | no | no | no | no | no | no | yes | not allowed |

Any combination not marked yes exits 3 with a message such as `chembl extract is not in SP2 - SP4 (structured serialize)`. Row counts and table effects: `docs/09-extraction-contract.md`.

### 4.3 Flags

- **`--dry-run`** is supported on `download` only; any other stage (including `all`) exits 3 with `... writes the DB - --dry-run is supported on 'download' only`. A dry run writes no files and skips the whole audit bracket (`dry-run: skipping run_start/run_end audit bracket`). It is **not** always offline: most wrappers still list or probe the upstream over the network (`curl` listings, mirror and release probes; `cdisc_bc` makes two GitHub API calls and `cdisc_usdm` makes three, all counting against the unauthenticated 60 requests per hour limit). The Hugging Face wrappers (`apollo`, `guidelines`, `hf_corpus`) only echo what they would do. `hf_corpus` with no repo id is a soft no-op (exit 0) under `--dry-run` and an error otherwise.
- **`--max-files N`** bounds the run; the meaning is per stage:
  - `download`: caps the resolved file set (`pmc` translates it to `--limit`; default `--limit 10` when absent, `0` means all). Honoured by every source except `apollo`, `guidelines` and `hf_corpus` (the Hugging Face CLI resumes by itself; the flag is ignored with an INFO line).
  - `extract` and `serialize`: caps the number of input files parsed (default from `EPISTEME_SAMPLE_LIMIT`, `0` = all).
  - `all`: applies to its download, extract and serialize stages.
  - `load`, `graph`, `enrich`, `materialize`: ignored.
  - `openalex` download, specifically: empty or `0` means unlimited (the whole `s3 sync`); a positive integer fetches the first N shard files in byte order (oldest `updated_date=` partitions first, `manifest.json` excluded); a non-integer such as `abc` or `-1` makes the wrapper die with its own exit code 2 when run directly; through `run_pipeline.sh` the observable rc is 1 (`stage failed: download`, `resume with: ...`).
  - `pubmed`: each data file is two entries (the `.xml.gz` and its `.md5`), so `--max-files 2` is one data file with its checksum.
- **`--no-wait`** is for a stage that needs a lock. If the lock is busy, the run does not wait. It exits 75. The runner exports `EPISTEME_NO_WAIT=1`, so a Python stage also refuses a busy advisory lock with exit 75 (section 4.8).
- **`--sample`** runs one `extract`, `serialize` or `load` stage on a small raw slice of a gated source. It writes the gate report and, if the rules pass, the pass file. Section 4.9.
- **`--skip-gate --reason "<why>"`** runs a gated stage that has no valid pass. It writes one `gate_override` audit row for each such stage. Section 4.9.
- **`--bulk`** is supported on `load` and `graph`: drop the secondary indexes of the stage's index group, run the stage, always rebuild them (section 4.6). `load` uses group `articles`; `graph` uses group `pubmed_graph` for every source. Any other stage exits with `--bulk is only valid with the load or graph stage`.
- **`--force --reason "<why>"`** re-does work that already has a success marker: extract and serialize reprocess, load reloads shards (the `load_articles` path records a `force_override` audit row with the reason), download wrappers delete and re-fetch the capped set. `--force` without a non-empty `--reason` exits 2 (`--force requires --reason`). Not every wrapper acts on `--force` (the Hugging Face wrappers and `europepmc_preprint` say so and ignore it). Forcing a reload is an audited exception: state a real reason.

### 4.4 Audit bracket and resume

Every non-dry run records `run_start` and, on completion, `run_end`, both with object = the source and a run id `<source>-<UTC timestamp>` shared by all stages of the invocation. On a stage failure the dispatcher records a `run_end` with reason `failed at stage <name>`, prints `resume with: run_pipeline.sh <source> <name>` and exits 1. A stopped stage (rc 130 or 143) gives the reason `stopped at stage <name>`, and the dispatcher exits with the same code. A stage lock that is busy under `--no-wait` gives the reason `refused: <kind> lock held by <run> (<stage>)` and exit 75 (section 4.8). A signal (rc 130 or 143) while the run waits for a stage lock gives the reason `stopped while waiting for the <kind> lock (stage <name>)`. A signal during the `--bulk` index drop or rebuild gives `stopped at stage <name> (bulk index drop)` or `stopped at stage <name> (bulk index rebuild)`. Any other unplanned exit after `run_start` gives `failed ...` in the same form. Each path writes at most one `run_end`, and one whenever `run_start` was written; a dry run writes none. A signal while a single-stage run waits for its lock comes before `run_start`, so it writes no `run_end` (only the status `stopped`). A failed `run_start` or `run_end` (database down, refused by the guard) is only a `WARN`, so a run can complete without its bracket; verify (section 7). Extract and serialize stages keep a per-file success marker and skip finished files; `load` keeps a per-shard `load_success` marker. Re-running a stage is therefore the resume mechanism.

### 4.5 Per-stage arguments (what the dispatcher forwards)

| Stage | Receives |
|---|---|
| `download` | `--max-files N`, `--force --reason R`, tokens it does not recognise; `--dry-run` as `EPISTEME_DRY_RUN=1` |
| `extract`, `serialize` | `--max-files N`, `--force`; with `--skip-unit`, also each `--skip-unit NAME` and `--reason R` |
| `load` | `--force --reason R`; with `--skip-unit`, also each `--skip-unit NAME` (and `--reason R` once) |
| `graph` | `--force --reason R` when forced (reloads files whose graph hash is unchanged; `pubmed` only acts on it) |
| `enrich`, `materialize` | nothing |

### 4.6 Bulk-load mode (`--bulk` on `load` and `graph`)

For a large load, `--bulk` drops the secondary indexes of an index group first and rebuilds them once at the end. Building an index once is much cheaper than updating it for every row:

```bash
bash scripts/data/run_pipeline.sh pubmed load --bulk
bash scripts/data/run_pipeline.sh pubmed graph --bulk
```

Index groups (`episteme.data.db.bulk_indexes.INDEX_GROUPS`):

| Group | Used by | Indexes dropped (on every partition) |
|---|---|---|
| `articles` | `<source> load --bulk` (the default group) | `articles_pmid_idx`, `articles_pmcid_idx`, `articles_doi_idx`, `articles_mesh_gin`, `articles_authors_gin`, `articles_publication_types_gin` |
| `pubmed_graph` | `<source> graph --bulk` | the 14 secondary indexes of `article_mesh`, `article_cites`, `article_comments`, `article_chemicals`, `article_keywords`, `article_authors` and `article_facets` (two each: the pmid or src_pmid index plus one lookup index) |

- **`graph --bulk` maps every source to `pubmed_graph`.** It is meant for `pubmed` (and `pmc`, which also writes `article_mesh` and `article_cites`). `mesh graph --bulk` would only waste an index rebuild.
- **Kept:** every `<table>_source_file_idx` (the per-file deletes use them), `articles_id_idx` and `articles_retrieved_at_brin`.
- **While the stage runs**, lookups on the affected tables are slow. Run nothing else against them.
- **Rebuild:** after the stage, `rebuild` re-creates each saved index on every partition and writes one `bulk_index_rebuild` audit row per index, in the same transaction as that index's build. `rebuild` takes no group: it rebuilds every pending row, whatever its group. `ANALYZE` runs after the last index and has no audit row. Expect tens of minutes on a full table.
- **Refusals:** `rebuild` refuses a saved definition that is not a parent index on an `episteme` table, contains `;`, or names a different index. It also refuses when an existing valid index of that name has a different definition. In each case it exits 1 and leaves that row pending. `drop` refuses (exit 2) while any rebuild is pending, whatever the group.
- **When it rebuilds:** always, after a successful, failed or interrupted (Ctrl+C) stage. The exit code is the stage's if it failed, else the rebuild's. A failed drop never starts the stage. A signal during the drop also rebuilds, because the drop may have finished.
- **Audit strings.** `bulk_index_drop` writes one row with object `<group> <comma-separated index names>`, for example `pubmed_graph article_mesh_pmid_idx,...`; before the groups existed the `articles` drop read `episteme.articles <names>`. `bulk_index_rebuild` writes one row per index with object `episteme.<table> <index>`.
- **Manual use.** `bash scripts/data/db/bulk_load_indexes.sh <action> [--group G]`: `drop` (default group `articles`; `--group articles|pubmed_graph`), `rebuild` (no group), `status` (exit 1 while a rebuild is pending, 3 on error). Use `rebuild` to finish after a failed rebuild.
- **Prerequisites:** migration 0005 (section 2.3), plus 0006 for the `pubmed_graph` group; `EPISTEME_ACTOR` (`drop` and `rebuild` both require it before doing anything) and `EPISTEME_SYS_ADMIN_PASSWORD`. The index work runs as `episteme_sys_admin`, the table owner, and the database-mode guard applies.

**After a hard kill** (Task Manager, `Stop-Process`, a crash), no trap runs and the indexes stay dropped. The saved state survives, and the next `--bulk` (either stage, any group) refuses to start (exit 2). Check and finish it:

```bash
bash scripts/data/db/bulk_load_indexes.sh status    # exit 0 when nothing is pending, 1 while a rebuild is pending, 3 on error, 2 if a required env key is missing
bash scripts/data/db/bulk_load_indexes.sh rebuild   # idempotent; safe to re-run
```

`status` exits 3 on an error, for example migration 0005 not applied (or 0006, for the `pubmed_graph` group), a refused connection or a guard-refused database. It exits 2 if a required env key (`EPISTEME_ACTOR`, `EPISTEME_SYS_ADMIN_PASSWORD`) is missing.

**Stopping runs:**

- **Extract (worker processes).** Stop the parent python *and its workers*: `taskkill /T /F /PID <parent pid>`. `Stop-Process` on the parent alone leaves the workers running. Files that finished keep their success markers; a re-run resumes from the rest.
- **A `--bulk` load or graph.** Stop the python load process, not the bash driver, so the driver's trap still rebuilds. If the driver was killed too, run `bulk_load_indexes.sh status`, then `rebuild`.

**Loader timing:** every `ok` line of a load ends with `read_sec=` (reading the shard and building rows), `copy_sec=` (inside `COPY`) and `rows_per_sec=` (over the whole shard, deletes included). When `read_sec` dominates, the client is the bottleneck; when `copy_sec` does, the database is.

### 4.7 Failure rules

These rules apply to the `extract`, `serialize`, `load` and `graph` stages. Downloads keep their own rule: record the failure and continue.

**Fail fast.** A stage stops at the first unit that fails. A unit is one input file (extract, serialize) or one shard (load). The stage does not start the next unit. With more than one worker, the units already running finish or fail. The exit code is 1.

**Stop.** A stop (Ctrl-C, SIGTERM, the parent watchdog) is not a failure. The stage cancels the queued units and terminates the workers in flight, then exits 130 or 143. A terminated unit has no success marker, so the next run does it again. The stage does not wait for the units in flight to finish.

How a stop reaches the stage:

- To stop a run, send Ctrl-C or `kill -TERM <runner PID>` to `run_pipeline.sh`. Do not use `taskkill /T` on the stage tree.
- `run_pipeline.sh` runs each stage in the background and waits for it. On INT or TERM it writes the signal name to the stop-request file (`EPISTEME_STOP_FILE`, `<run folder>/stop.request`). Bash cannot signal a native Windows Python, so the file carries the stop. The file also carries Ctrl-C: a background stage can ignore SIGINT.
- The parent watchdog of the stage reads the file every second. When the file appears, the watchdog stops the stage as a SIGTERM does: the stage cancels its statement and exits 143. If the stage is in a long statement and still runs 5 seconds later, the watchdog cancels the statement again.
- The runner waits for the stage to end. Then it writes the end state `stopped` and the `run_end` row, and releases the lock. The runner exits 130 (Ctrl-C) or 143 (SIGTERM). A second Ctrl-C does not make the runner exit early.
- A stop request is for the stage that runs now only. The runner removes an old stop-request file before each stage starts. After a stop, the Python steps of the EXIT handler (the `--bulk` index rebuild) do not see the request, so they run to their end.
- If the runner itself dies (a hard kill), the watchdog sees that the runner is gone within 10 seconds. It cancels the statement, terminates the child processes of the stage (the pool workers), and exits 143. The `heavy.lock` stays; the next run takes it over as stale.
- A stage that hangs and does not stop: end it with `taskkill /T /F /PID <stage PID>` (the `stage` PID in `status.json`). The runner then ends as well.

**Retries.** A transient error is retried 3 times. The waits are 30, 60 and 120 seconds. A load or graph unit takes its connection for each attempt from the pool. That connection can be a healthy idle one that the pool reuses. After a server restart, the pool can hand out dead idle connections, and each one uses up an attempt. If the third retry fails, the error is permanent and the stage stops.

- Transient: a PostgreSQL connection error (SQLSTATE class `08`), serialization failure (`40001`), deadlock (`40P01`), lock not available (`55P03`), operator intervention (`57P01` to `57P05`) and too many connections (`53300`). Also transient: a connect error with no SQLSTATE, and OS errors such as I/O error, time-out, connection reset or refused, a broken pipe, and the Windows codes 21, 64, 121, 1117 and 1231.
- Permanent: all other errors. A cancelled query (`57014`) is permanent. So are a missing file, a permission error and a bad row.
- A wrong password is a connect error with no SQLSTATE. It is treated as transient. The stage waits about 3.5 minutes (30 + 60 + 120 s) before it fails. Fix the password first, or stop the stage and start it again.

**`last_failure.json`.** A stage that stops on a unit writes `<processed root>/_ops/<source>/last_failure.json`. The file holds `run_id`, `stage`, `unit`, `error` and `at`. `run_pipeline.sh` puts the unit and the error in the `run_end` audit reason. To read the record:

```bash
python -m episteme.ops.failure show --source <source> --run-id <run_id>
```

The command prints `<unit>: <error>`. It prints nothing when the record belongs to another run. The unit name is what `--skip-unit` takes.

**Resume.** Run the same stage again:

```bash
bash scripts/data/run_pipeline.sh <source> <stage>
```

Units that are done are skipped. Extract and serialize use the success markers. Load and graph use the load and graph markers. The failed unit runs again.

**Skip a unit.** Use this only when a unit fails for a permanent reason and you accept to leave it out.

```bash
bash scripts/data/run_pipeline.sh <source> <stage> --skip-unit NAME --reason "TEXT"
```

- `--skip-unit` can be repeated. `--reason` is required. Only one named stage is allowed: `extract`, `serialize` or `load`. `all` and `graph` exit 2. Exception: the `load` stage of `europepmc_id_mappings` and `cdisc_ct` does not take `--skip-unit` and exits 2 (their loaders are not yet under these rules).
- The skip is for this run only. No marker stays. The next run tries the unit again.
- Each skipped unit writes one audited `unit_skip` row with your reason.
- NAME is the unit name from `last_failure.json`. For extract and serialize it is the `input_key` (the path under the raw directory). For load it is the shard file name.
- A bare file name works when exactly one input has that name. If two inputs share the name, use the `input_key`.
- A shard that already has a success marker is skipped as a `unit_skip` too, when you name it.
- A name that matches no unit exits 2, and nothing is audited. Under `run_pipeline.sh` the runner then exits 1 (see the exit codes below).

**Graph: a file that fails for a permanent reason.** The `graph` stage takes no `--skip-unit`. A failed file writes no `_graph_lineage` row, so every re-run tries it again. `--force --reason` does not help: the failing file fails again. What helps depends on the cause:

- The success marker or the relation files on disk are bad (a pre-1.6 marker, an unreadable marker, a missing or broken relation file). Rebuild them:
  1. Remove the file's extract success marker: `<processed root>/_ops/pubmed/success/<unit>.ok`.
  2. Run `extract` again. It redoes only that file.
  3. Run `graph` again.
- The raw input file is bad. Download it again first, then do the three steps above.
- In every other case, the graph stage stays blocked at that file. A graph `--skip-unit` is in the backlog.

**Exit codes.**

| Code | Meaning |
|---|---|
| 0 | The stage finished. |
| 1 | A unit failed (permanent, or transient after 3 retries). |
| 2 | Usage error, for example `--skip-unit` without `--reason`. |
| 130 | Stopped with Ctrl-C. The audit `run_end` reason is `stopped at stage <name>`. |
| 143 | Stopped with SIGTERM, or the parent runner died. |
| 75 | Refused: a lock is busy and `--no-wait` is set (section 4.8). `migrate_database.sh` and `bulk_load_indexes.sh` run directly always wait, so they never exit 75. In a `run_pipeline.sh` run with `--bulk` and `--no-wait`, the index drop exits 75 when the advisory lock is busy; the rebuild always waits. |

When a stage fails, `run_pipeline.sh` passes 130, 143 and 75 through and turns every other non-zero stage code into 1. So exit 2 and 3 come from its own argument checks before `run_start`, or from the `--bulk` index drop or rebuild, which keep their own code (section 4.6). For example, a `--skip-unit` NAME that matches no unit makes the stage exit 2 and `run_pipeline.sh` exit 1. The `run_end` reason still names the stage.

**Stop handling.** Ctrl-C and SIGTERM stop the stage at once. A load or graph stage first cancels the running SQL statement.

- With 1 worker, the unit in progress gets no success marker, so a re-run does it again.
- With more than 1 worker, a SIGTERM to the stage lets the units already in flight finish and write their success markers. The queued units do not start. A console Ctrl-C also reaches the pool workers, so units in flight can stop with no marker; a re-run redoes them.

To stop a stage, stop the process (Ctrl-C or SIGTERM), or cancel its statement (`pg_cancel_backend`, SQLSTATE `57014`). Do not use `pg_terminate_backend`. It gives `57P01`, which is transient, so the stage waits 30 seconds and retries.

**Parent watchdog.** Each stage runs a watchdog thread. It watches one process:

1. The runner. `scripts/data/_lib/common.sh` exports `EPISTEME_RUNNER_PID`. The outermost script sets it: under `run_pipeline.sh` it is the PID of `run_pipeline.sh`; a wrapper run directly gives its own PID. Under Git Bash the value is the Windows PID (`/proc/$$/winpid`).
2. If `EPISTEME_RUNNER_PID` is not set, is not an integer, or names a process that is not running when the stage starts, the stage logs a WARNING. It then watches its first ancestor that is not the venv launcher (`.venv/Scripts/python.exe` on Windows). On Linux that is the parent process. If no such process can be found, the stage logs a WARNING (`no process to watch`) and runs without a watchdog.

The watchdog checks the process every 10 seconds. It compares the process start time too, so a reused PID counts as gone. When the process is gone, the watchdog cancels the running statement (it waits at most 10 seconds for the cancel) and exits with 143. So the stage notices a dead runner within about 10 seconds, and exits within about 20.

On Windows, a hard kill of the runner (`taskkill /F`) leaves the stage's bash stub and the venv launcher alive. Only the runner PID lets the stage notice. Start stages through the wrapper scripts or `run_pipeline.sh`, so `common.sh` sets it.

A load that is run directly (not under `run_pipeline.sh`) owns its audit bracket: it writes its own `run_start` and `run_end`. If the watchdog exits such a load with 143, the `run_end` is not written, because `os._exit` skips it. The audit trail then holds a `run_start` with no `run_end`. Under `run_pipeline.sh` the runner owns the bracket, not the load.

**Orphan sweep.** Each connection sets `application_name` to `episteme:<run_id>:<pid>`. At the start of a load or graph stage, the sweep looks for backends that fit these rules:

- the `application_name` starts with `episteme:`;
- the backend belongs to the same database role;
- the backend is `active`;
- the client PID in the name no longer exists on this host.

The sweep cancels the running statement of each such backend. Each cancel writes an audited `orphan_cancel` row.

**Known limits.**

- A PID that another process has reused makes an orphan look alive. The sweep leaves it alone.
- The sweep checks PIDs on this host only. It assumes the clients run here.
- The sweep does not touch an orphan that is `idle in transaction`. That backend ends only when the server notices the closed socket.
- Downloads do not use these rules. They record a failed file and continue (section 4.8).

### 4.8 Locks, status and the queue

One heavy job runs at a time on this machine. A download may run beside a heavy job only until the heavy job needs the disk. The locks enforce this.

**Which lock a stage takes.**

| Stage | Lock |
|---|---|
| `download` | the download lock |
| `extract`, `serialize`, `load`, `graph`, `enrich`, `materialize` (also `corpus materialize`) | the heavy lock |

- `migrate_database.sh` and `bulk_load_indexes.sh drop` or `rebuild` also take the heavy lock. `bulk_load_indexes.sh status` takes none.
- A `--dry-run` takes no lock and writes no status.
- A single stage takes its lock before `run_start` and holds it until the exit. With `all`, each stage takes its own lock and releases it at its end. A waiting job can run between two stages of an `all` chain.
- The Python stage (extract, serialize, load, graph) re-enters the lock of its wrapper. The run id and the runner PID are the same, so it does not wait for itself. A stage run directly, without `run_pipeline.sh`, takes the heavy lock itself with the run id `adhoc-<runner pid>`.
- `load`, `graph`, the `cdisc_ct` retire step and the `--bulk` index drop and rebuild also take a PostgreSQL advisory lock. Its key is `4994572608998821189`. It is per database. It is held on its own connection, and the server frees it when the process ends. A session that holds it makes this stage wait. While the stage waits for it, the status shows `long_statement` with the label `waiting for the advisory lock`, so `--stalled` uses the 60-minute limit for it, not 10 minutes. Under `--no-wait`, a busy advisory lock makes the stage exit 75.

**Waiting and refusing.**

- Without a flag, a job waits while the lock is busy. It checks every 5 seconds. It logs `waiting for the heavy lock: held by ...` at the start and every 5 minutes. When a running download blocks it, the line is `waiting for the heavy lock: download <run> (<stage>) has no pause point or has not reached one`. No order is promised between two waiting jobs.
- While `run_pipeline.sh` waits for a stage lock, the status shows `state` `running`, the stage that waits, and `unit` `waiting for the <kind> lock`. `--stalled` does not report such a wait as a stall (only a gone runner, as `crashed`).
- A heavy job that waits writes `heavy.request`. A running download sees it and pauses.
- A heavy job waits while a download is `running`. It does not wait for a download that is `paused`.
- A second download waits for the first.
- With `--no-wait`, a busy lock does not make the run wait. The run logs `refused: <kind> lock held by <run> (<stage>)`, sets the status `refused` and exits 75. A single stage writes no audit row. In an `all` chain, the earlier stages stay done, and the run writes `run_end` with the refusal as the reason.
- `migrate_database.sh` and `bulk_load_indexes.sh` have no `--no-wait`. Run directly, they always wait for the heavy lock and the advisory lock, so they never exit 75. In a `run_pipeline.sh` run with `--bulk` and `--no-wait`, the index drop sees `EPISTEME_NO_WAIT=1` and exits 75 when the advisory lock is busy (status `refused`). The rebuild always waits, so a refusal never leaves the indexes dropped.

To refuse instead of wait, add `--no-wait`:

```bash
bash scripts/data/run_pipeline.sh pubmed load --no-wait
```

**The lock files.** They are in `<data root>/_ops/locks/`. `EPISTEME_LOCK_DIR` overrides the folder.

| File | Meaning |
|---|---|
| `heavy.lock` | A heavy job holds the heavy lock. |
| `download.lock` | A download holds the download lock. Its `state` is `running` or `paused`. |
| `heavy.request` | A heavy job waits for the heavy lock. |

Each file is JSON with `kind`, `pid`, `create_time`, `run_id`, `stage`, `started_at` and `state`. Show them with:

```bash
python -m episteme.ops.lock status
```

The command prints one line per file. The line is `free`, or the holder (`run <id> (<stage>, pid <pid>, since <time>)`), with `stale` in front when the holder is gone.

**The stale rule.** The owner of a lock is a PID plus the create time of that process.

- The lock is stale when the PID is not running, or when the process with that PID has a different create time (Windows reused the PID).
- A file that cannot be parsed is stale after 60 seconds.
- A file that another process holds open (Windows) counts as live. The lock fails closed.
- A job that finds a stale lock deletes it and takes it. Only one job at a time does this. It logs `WARNING took over a stale heavy.lock from run ...`. The same happens after a hard kill: the lock stays, and the next run takes it over.
- Limit: a lock whose PID is alive but wrong is not detected as stale. This happens when the run took a wrong inherited `EPISTEME_RUNNER_PID`. Two direct runs that inherited the same wrong `EPISTEME_RUNNER_PID` get the same `adhoc-<pid>` run id and owner. The locks do not keep them apart: the second run re-enters the lock of the first.
- A process that this user may not inspect (access denied) counts as alive while its PID exists. The lock fails closed.

**Clear a lock by hand.** Do this only when:

1. `python -m episteme.ops.lock status` shows the lock as `stale`, or
2. you confirmed that the owner process is gone (for the wrong-PID case above).

Then delete the lock file. Never delete the lock of a live job.

**Download pauses.** A download pauses when a heavy job holds the heavy lock or waits for it. It pauses between units of work, never in the middle of one. While it pauses, `download.lock` has the state `paused`. When the heavy job has ended, the state returns to `running`.

| Download | Pause point |
|---|---|
| `http_fetch` (the aria2c and curl wrappers) | before each batch of `EPISTEME_FETCH_BATCH` URLs (default 64) |
| `pmc` (`download_pmc.py`) | before each submit batch of up to 2,000 articles |
| `s5cmd` and `aws s3 sync` (S3 sources) | none |
| `hf download` (Hugging Face sources) | none |
| `europepmc_preprint` (the REST harvester) | none |

For the downloads with no pause point, a waiting heavy job waits until the download ends. A pause point also needs a run id (`EPISTEME_RUN_ID`). Any download run outside `run_pipeline.sh` (a wrapper or the `pmc` downloader run directly) has no run id. It takes no download lock and never pauses.

**`failed_files.txt`.** A download records a file that failed and goes on with the next one. At the end, the stage exits 1 if any file failed.

- The file is `<processed root>/_ops/runs/<run id>/failed_files.txt` (the folder is `EPISTEME_RUN_DIR`). Without a run folder, `http_fetch` writes `.failed_files.txt` in the download folder, and it starts a new one at each run.
- A line of `http_fetch` is the URL, the relative path and the reason, separated by tabs. A line of the `pmc` downloader is the PMCID and the error, separated by a tab.
- To retry, run the same download again: `bash scripts/data/run_pipeline.sh <source> download`. A file that is complete is skipped. A failed file is fetched again. The `pmc` downloader tries a failed article again, because nothing marks it done.

**Status files.** Each non-dry run writes `<processed root>/_ops/runs/<run id>/status.json`.

| Field | Meaning |
|---|---|
| `run_id`, `source`, `stage` | The run and its current stage. |
| `state` | `running`, `done`, `failed`, `stopped` or `refused`. |
| `unit`, `units_done`, `units_total`, `eta_s` | The unit in progress and the count of units. |
| `rows_done`, `rate_rows_per_s` | Rows so far and their rate (stages that count rows). |
| `started_at`, `last_progress`, `heartbeat`, `updated_at` | `heartbeat` shows that the process is alive (every 30 seconds). `last_progress` shows that work moved: a unit, a batch or a phase ended. |
| `long_statement` | `label` and `since` while one long step runs (an aria2c batch, a long SQL statement). |
| `pids` | The PID and create time of the runner and of the stage process. |
| `reason` | Why the run is `failed`, `stopped` or `refused`. |

A status write that fails is logged as a WARNING and dropped. It never stops a stage. The runner writes the end state after the stage, so its value wins. A stage that is run directly writes its own end state.

**Which stages report progress.** These stages write `last_progress`:

- `extract` and `serialize`, through `run_per_file` (one record per input file);
- `load` (one record per shard);
- the `pubmed` graph (one record per file);
- the other graphs: the build is one transaction, marked as one long statement (`graph build <source>`);
- `bulk_indexes` drop and rebuild (each index is one long statement);
- `http_fetch` downloads (one record per batch);
- the `pmc` downloader (one record per submit batch).

These stages report no progress: the `s5cmd`, `aws` and `hf` downloads, the `europepmc_preprint` downloader, `materialize`, `enrich` and `europepmc_id_mappings load`. For them, a `--stalled` result after 10 minutes means only "no progress signal". Check the process before you act.

Show the runs, one line each (run id, source, stage, state, unit, done/total, `last_progress`):

```bash
python -m episteme.ops.status
python -m episteme.ops.status --stalled
```

`--stalled` prints only the runs that are not finished and look stuck. It exits 1 if it finds one, else 0. A run is stalled when:

1. its runner process is gone (`crashed`), or, for a run with no runner entry (a stage run directly), its stage process is gone, or
2. it made no progress for 10 minutes, or
3. it made no progress for 60 minutes while `long_statement` is set.

A run in `done`, `failed`, `stopped` or `refused` is never stalled. Nor is a run that waits for a stage lock (`unit` `waiting for the <kind> lock`), unless its runner is gone. The heartbeat does not count as progress.

**The queue.** `run_queue.sh` runs several `run_pipeline.sh` steps one after the other.

```bash
bash scripts/data/run_queue.sh "pubmed:extract,load,graph" "pmc:extract"
```

- Each argument is `<source>:<stage>[,<stage>...]`. The steps run in the order given. The script checks every argument first. If one is wrong, nothing runs and the exit code is 2.
- A step is done when `run_pipeline.sh` exits 0 and the step log holds the line `<time> [INFO] pipeline done: <source> <stage>` as a whole line. Any other result stops the queue with `STOPPED at <source> <stage> (rc=<n>)` and exit 1. The queue does not skip a step and does not retry it.
- Before each step, the queue compares the code with the code at the start: the git `HEAD`, the content of the changes in `scripts/data` and `src` (the diff against `HEAD`), and the names and content of the untracked files there. An edit to a file that was already changed at the start counts too. If it differs, the queue stops with `STOPPED at <source> <stage>: code changed since the queue started` and exit 1. Without git, the queue logs a WARNING and runs without this check.
- The queue takes no lock and writes no status file. Each step does. Each step waits for a busy lock.
- The logs are in `<processed root>/_ops/queue/queue-<UTC time>/`: `queue.log` and one `NN-<source>-<stage>.log` for each step. Print the folder with `python -m episteme.ops.status queue-dir --queue-id <id>`.
- After a stop, find the cause in the log of the last step. Fix it, then start a new queue with the steps that remain. Units that are done are skipped.
- Exit 0: all steps done. Exit 1: the queue stopped. Exit 2: usage error.

### 4.9 The sample gate

A full run of a gated stage needs a valid pass. The pass comes from a sample run: the same stage, run on a small slice of the raw files. The gate measures the output of the sample and checks it against the rules of the source. A source that fails the rules gets no pass.

**What is gated.** A source is gated for a stage when `src/episteme/ops/gate_rules/<source>.json` has a section for that stage. These sources have a rules file:

| Source | Gated stages | Slice of the raw files | Slice cap |
|---|---|---|---|
| `bookshelf` | `extract`, `load` | Only packages of at most 1 GB. Up to 20 packages spread evenly through the list (all of them if there are 20 or fewer), plus the package at the 99th percentile of size. | 2 GB |
| `europepmc_manuscript` | `extract`, `load` | The first baseline package by name, the newest baseline package by date, the newest incremental package by date, and the `.filelist.csv` or `.filelist.txt` files of each. | 1 GB |
| `apollo` | `extract`, `load` | In each of three groups, the smallest file of at least 10 MB. The groups are `train/pretrain` `*_qa.json`, `train/pretrain` `*_text.json`, and `train/sft` files whose name starts with `medical`. | 4 GB |

- A source with no rules file is not gated. `pubchem` has none: its slice needs the parts of sub-project 2.
- The stages `download`, `graph`, `enrich` and `materialize` are not gated.
- A slice keeps the path of each file relative to the raw folder of the source. The `input_key` and the row ids are therefore the same as in a full run.
- If a group has no file, a file is missing, or the slice is over its cap, the run refuses (exit 2).

**Run a sample.** Run the extract, then the load. Each command takes the stage lock like a normal run.

```bash
bash scripts/data/run_pipeline.sh bookshelf extract --sample
bash scripts/data/run_pipeline.sh bookshelf load --sample
```

- `--sample` needs one named stage (`extract`, `serialize` or `load`). It does not accept `all`, `--bulk`, `--force` or `--dry-run`.
- `--sample` does not accept `--max-files` or `--skip-unit`. The slice is the file selection of a sample run. With one of these flags the run exits 2 with `--sample cannot be used with <flag>`.
- `--sample` needs the key `PGDATABASE_SECONDARY` in the environment. Without it the run exits 2.
- A sample `extract` builds the slice first. It deletes the earlier sample output of that source and starts clean.
- A sample `load` loads every shard of the sample staging again. It needs the sample extract first. Without the sample staging it exits 2.
- Before a sample `load`, the runner checks the secondary database (see "The migration check").
- A sample run that fails a rule exits 1 with `sample gate: no pass for <source> <stage>`. The failed rules are printed above that line.
- When a sample run fails its rules, the runner deletes the old pass of that source and stage.

**Where the files are.** All sample files are in `<processed root>/_sample/<source>/`. The pass is in the production processed root.

| File | Content |
|---|---|
| `<processed root>/_sample/<source>/slice.json` | The files of the slice (`files`), the units of work of the stage (`inputs`), their total size, and how many were hard-linked or copied. For `europepmc_manuscript`, `inputs` holds the `.tar.gz` archives only, not the `.filelist` files. |
| `<processed root>/_sample/<source>/01_raw/`, `02_processed/`, `03_corpus/` | The sample input and output. |
| `<processed root>/_sample/<source>/run_record_<stage>.json` | The time, the exit code and the peak memory of the stage, and the code hash and rules hash taken before the stage started. |
| `<processed root>/_sample/<source>/02_processed/_ops/runs/<run id>/status.json` | The run status of the sample run. It is under the sample processed root, not under the production `_ops/runs/`. |
| `<processed root>/_sample/<source>/gate_report.md` and `gate_report.json` | The report of the last sample run. |
| `<processed root>/_ops/<source>/gate/<stage>.pass` | The pass. |

The slice uses a hard link to each raw file where the file system allows it. Otherwise it uses a copy. The runner never deletes anything outside `<processed root>/_sample/`.

**What a sample run changes.**

- It sets `EPISTEME_DB_TARGET=secondary`. The `run_start` and `run_end` audit rows and the load go to `episteme_test`.
- It sets `EPISTEME_DB_MODE=restricted`, whatever the shell or `.env` says (also `unrestricted` or `read-only`). The DB-mode guard then refuses the production database, also when `PGDATABASE_SECONDARY` names it by mistake. `episteme_test` is allowed in this mode.
- It redirects only the raw, processed and corpus roots, to the sample folder. The data root and the lock folder stay as in production. A sample run and a production heavy job therefore never run at the same time.

**The migration check.** Before a sample `load`, the runner checks that `episteme_test` has the objects of the migrations 0002 to 0009. It checks the objects, not `episteme._migrations`: production has an empty `_migrations` table, because the migrations were applied by hand (section 2.3). Migration 0001 is deferred: pgvector is not on the server. The check prints each migration that lacks an object. Then the run exits 2. If the check itself fails (for example, the database is not reachable), the run exits 1.

To add the missing migrations, use the per-file procedure of section 2.3. Do it once, and only with the operator's go-ahead.

1. Apply 0002 to 0009 to `episteme_test` as `episteme_sys_admin`, with the loop in section 2.3.
2. Apply 0004 with no load running: its `CREATE INDEX` blocks writes to every partition while it builds.
3. Run the check query of section 2.3.

Do not use `migrate_database.sh` on the local build: it stops at 0001 (pgvector) and applies nothing after it.

Migration 0004 drops its indexes on purpose during a bulk load. The check can show 0004 as missing while a bulk load runs.

**What the pass holds.** The pass file holds the package version, the git commit, the time, the path of the report, and two hashes:

- The code hash covers every `episteme` module that the stage imports (also modules that a function imports inside its body) and the wrapper script of the stage. It does not cover third-party packages, data files or other scripts. The runner itself (`scripts/data/run_pipeline.sh` and `scripts/data/_lib/common.sh`) is not covered.
- The rules hash covers the rules file.

The runner takes both hashes once, before the stage starts (`gate run`). The pass holds these hashes. If the code or the rules change before the report, the sample gets no pass, and the report shows `code_hash: the stage code changed during the sample run` or `rules_hash: the rules changed during the sample run`.

A change in the code of the stage, in its wrapper, or in the rules file makes the pass invalid. The `load` stage of every source uses `episteme.data.load_articles` and the wrapper `load_<source>.sh`. A change to the loader therefore invalidates the load pass of every gated source.

**What a refusal looks like.** A full run of a gated stage without a valid pass exits 2 before `run_start`. The log shows, for each such stage, `gate: no valid pass for <source> <stage> (<reason>)` after the time stamp and `[ERROR]`. The last line is `gate: refused; run '<script> <source> <stage> --sample' until it passes, or use --skip-gate --reason REASON`. With `all`, the runner checks each stage of the chain. The reason for a missing pass is `no pass`, `code changed` or `rules changed`.

When a sample run fails its rules, the runner deletes the old pass of that source and stage.

**Skip the gate.** Use this only when a pass is not possible. A reason is mandatory.

```bash
bash scripts/data/run_pipeline.sh bookshelf extract --skip-gate --reason "<why>"
```

- The runner writes one `gate_override` audit row for each gated stage that has no valid pass. The event is described in `docs/11-gxp-data-integrity.md`.
- The runner writes the row before `run_start`. It needs the database for this. If the write fails, the run stops with exit 1.
- The row goes to the production database. The write therefore needs the same DB mode as the full run itself: `EPISTEME_DB_MODE=unrestricted`. In `restricted` mode the guard refuses the write, and the run exits 1.
- `--skip-gate` cannot be used with `--sample`.

**Read `gate_report.md`.**

1. Open `<processed root>/_sample/<source>/gate_report.md`.
2. Read the line `Result: PASS` or `Result: FAIL`.
3. Read the list `Failed rules`. Each line names the rule, the measure and the limit.
4. Read the table `Measures`. It shows every measure, also those that no rule reads.
   The table rounds decimals to 6 significant digits. The rules compare the exact values: 1 empty text in 25,000 rows (0.004 %) fails `max_empty_text_pct: 0`. `gate_report.json` holds the exact values.
5. A line `<rule>: the measure is missing` means the gate could not compute the measure. A missing measure always fails.
6. A line `run record: the command exited with rc N` means the stage itself failed.
7. A line `code_hash: ...` or `rules_hash: ... changed during the sample run` means the code or the rules changed while the sample ran. Run the sample again.
8. A line `run record: no code hash` means the run record does not come from `run_pipeline.sh --sample`. Run the sample with the runner.

The main measures:

| Measure | Meaning |
|---|---|
| `rows`, `rows_per_input` | Rows in the sample staging, and rows for each unit of work (the `inputs` of `slice.json`). |
| `text_bytes_per_input_byte` | Bytes of `text` for each byte of input. |
| `duplicate_ids` | Rows minus distinct `id` values (`distinct_ids`). |
| `duplicate_ids_in_shard` | Duplicate `id` values inside one staging shard. The loader cannot load these: the `article_body` key `(article_id, source)` stops the shard. A duplicate across two shards (an id that a later archive issues again) loads once. |
| `empty_text_pct` | Percent of rows with no text in `text`, `body_text` or `abstract`. |
| `extract_status_pct` | Percent of rows for each `extract_status` value. |
| `null_pct` | Percent of rows with an empty value, for each article column. |
| `license_counts` | Rows for each `license` value. |
| `failed_inputs` | Inputs that failed. It is 0 when the stage exits 0. |
| `rows_loaded`, `orphan_bodies`, `rows_loaded_equals_staging` | Load only. The loaded rows of the slice, the `article_body` rows of the source that have no `articles` row, and whether the loaded rows equal the distinct staging ids (`distinct_ids`). The loader keeps one row for each id: a re-issued id under another `source_file` replaces the older row. `orphan_bodies` covers the whole source, so leftover rows of earlier samples count. |
| `elapsed_s`, `peak_rss_mb`, `rows_per_s` | The cost of the stage. No rule reads them. Use them to plan the full run. |

**Change a threshold.** Edit the rules file. This invalidates the pass of the source (the rules hash changes), so run the sample again. Write the reason for the change in the drift log.

The gate refuses a rules file that names something it does not know:

- a stage that the gate cannot hash for the source (only `extract` and `load` for the three gated sources),
- a rule key that is not in the list of rules,
- a `max_null_pct` column that is not an article column (`ARTICLE_COLUMNS` in `src/episteme/data/article_schema.py`),
- a `max_status_pct` status other than `ok`, `empty`, `partial` or `dropped`.

`gate check` then exits 2. A full run and a sample run of that source then refuse with exit 2. The log names the file and the unknown name.

**Exit codes.**

| Exit | Case |
|---|---|
| 1 | An operational failure before `run_start`: a slice I/O error, a failed migration check, a failed `gate_override` write, the processed root cannot be read, or a `gate check` that crashes in a sample run (an answer that the runner does not know). Also a sample run that fails its rules or whose code or rules changed during the run, after the stage ran. |
| 2 | A refusal before `run_start`: no valid pass, a slice refusal, missing migrations, a `--sample` on a stage with no rules, a `--sample` load without sample staging, a bad flag combination (including `--sample` with `--dry-run`, `--max-files` or `--skip-unit`), `--sample` without `PGDATABASE_SECONDARY`, or a rules file that the gate refuses (`gate check` exits 2). |
| 3 | A `--dry-run` of a gated stage. It stops at the existing dry-run guard before any gate check. |

---

## 5. Sources

Each block gives what it is, where it lands, the first-time commands, the incremental commands, and known failures. Licence and cadence per source: `docs/12-source-inventory.md`. All commands carry `PGDATABASE=episteme_test`; downloads need internet access. "Incremental" means: rerun the same commands. Downloaders skip size-matched files; extract, serialize and load skip files that already have a success marker.

### 5.1 Literature (download, extract, load, graph)

Row contract and status rules: `docs/09-extraction-contract.md`. Load and graph need migration 0002; `pubmed` graph also needs 0006 (section 2.3).

#### PubMed (`pubmed`)

Citation and abstract XML: baseline (annual) and daily update files, from `PUBMED_FTP_BASE`. Not full text. Raw: `<raw>/pubmed/baseline/` (mode `baseline`, the default), `<raw>/pubmed/updates/` (mode `updates`, the upstream `updatefiles` directory) and the published checksums under `<raw>/pubmed/md5/`.

```bash
# first time (bounded): one baseline file plus its checksum
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed download --max-files 2
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed extract --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed load
# graph: mesh first (it fills mesh_tree, which pubmed graph requires), then pubmed
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh mesh graph
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed graph

# whole baseline in one go (large; unbounded)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed all

# incremental: daily update files
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed download updates
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed extract
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed load
```

**Graph and facets (schema 1.6).** `extract` writes the six relation files next to each shard (docs/09 section 4.5). `pubmed graph` loads them per raw file in one transaction: it deletes that file's earlier edge and facet rows, COPYs the six relations, derives the file's facets and writes one `graph_commit` audit row plus the `_graph_lineage` row. A failed file rolls back alone (an unreadable, e.g. truncated, success marker fails that file with `unreadable success marker`). The stage then stops at that file and exits 1 (section 4.7); later files are not touched. If no pubmed graph files are found at all, it prints `WARNING: no pubmed graph files found under <processed_dir>` to stderr and exits 1. The wrapper `graph_pubmed.sh` passes no directory arguments: the processed root comes from `config.py` (`EPISTEME_PROCESSED_ROOT`, else `EPISTEME_DATA_ROOT/02_processed`), the same as extract; a direct `--processed-dir` after the wrapper still overrides it. A file whose `graph_sha256` in `_graph_lineage` is unchanged is skipped; `pubmed graph --force --reason "<why>"` reloads it and is audited (`force_override`); `run_pipeline.sh` forwards `--force --reason` to the graph wrapper. `pubmed graph` refuses to start when `mesh_tree` is empty (run `mesh graph` first), and refuses a shard with no graph info (`re-extract with schema >= 1.6`). `--force` is only acted on for `pubmed`; for any other graph source it prints `NOTE: --force has no effect for source <src> graph` to stderr and nothing is audited. `extract` redoes any file whose success marker lacks `graph_relations` even without `--force`; a file with a 1.6 marker is skipped. Add `--bulk` for a full-baseline load (section 4.6).

MeSH facets (`condition`, `test`, `demographic`, `special_population`, `drug`) live in `article_facets`, a plain table kept current per file (no materialized views). The rules are rows in `mesh_facet_rule` (rule_version 1 is seeded by migration 0006; edit them as `episteme_sys_admin` only, and bump `rule_version`). After a rule change or a new MeSH release:

```bash
PGDATABASE=episteme_test .venv/Scripts/python.exe -m episteme.data.graph_builder facets --status
PGDATABASE=episteme_test .venv/Scripts/python.exe -m episteme.data.graph_builder facets --rebuild
```

`--status` prints `rule_version`, `stale_rows`, `stale_files` and `files`, and exits 1 when any `article_facets` row carries an older `rule_version` than the newest rule, else 0. `--rebuild` recomputes one source file per transaction, with one `graph_commit` audit row per file (and a `force_override` row per file with `--force`); it is restartable (files already at the newest `rule_version` are skipped), and `--rebuild --force --reason "<why>"` redoes every file. `--force` without `--reason` exits 2.

**Data-quality audit.** Read-only, no database access (DuckDB over the shards and graph files):

```bash
.venv/Scripts/python.exe -m episteme.data.audit.pubmed_audit [--processed-dir DIR] [--raw-dir DIR] [--out-dir DIR] [--sample-files N] [--memory-limit 2GB] [--duckdb-temp DIR]
```

It writes `pubmed_audit.json` and `pubmed_audit.md` under `<processed>/_ops/pubmed/audit/` (override with `--out-dir`) and prints the report path. `--sample-files N` (default 5) is the number of raw files re-read for the DOI probe, which compares each record's stored DOI with the record's own `ArticleIdList`. The default `--memory-limit` is 2GB, deliberately below the profile's DuckDB cap (about 4 GB on a 16 GB machine) so the audit can share the machine with PostgreSQL; `--duckdb-temp` is the spill directory. The report also carries `graph_btree_max_bytes` (the longest value in bytes per btree-indexed graph column: `mesh.descriptor_ui`, `cites.src_pmid`, `cites.dst_pmid`, `comments.ref_pmid`, `chemicals.substance_ui`, `keywords.keyword`, `authors.orcid`; keys `columns`, `max`, `limit` = 2000 and `over_limit`) and `shards_without_graph` (pubmed shards whose success marker lacks `graph_relations` or whose six graph files are not all present). Exit codes: 0 pass; 1 when any btree column exceeds 2000 bytes, when `shards_without_graph` is above 0, when the DOI probe did not run (its status is not `ok`, for example no raw directory) or found a mismatch; `--sample-files 0` skips the probe, so only the first two conditions can fail it. Each failing condition prints a `WARNING:` line. Treat exit 1 as a failed gate.

**Rollout order for the full baseline** (spec `docs/superpowers/specs/2026-10-14-pubmed-graph-design.md`):

1. Finish any pending index rebuild (`bulk_load_indexes.sh status` reports `pending=0`).
2. Apply migration 0006 (section 2.3, including the operator REVOKE).
3. Trial on one file (`pubmed26n1300`): extract it, run the audit, graph it; inspect the graph files and the DOI fix.
4. Re-extract all 1,334 files with `--force --reason "schema 1.6"`.
5. Audit again. Stop here for review before loading.
6. `pubmed load --bulk --force --reason "schema 1.6: DOI and retraction fixes"`.
7. `mesh graph`, then check the seed descriptor UIs of `mesh_facet_rule` against the production `mesh` rows (read-only; the fixtures hold no real MeSH), then `pubmed graph --bulk`.
8. Verify: the audit chain (`verify_audit_trail.sh`), every index valid with its `bulk_index_rebuild` audit row, `facets --status`, `neighbours` for each kind, `articles_for_mesh` with descendants, and facet counts per facet.

Verify downloaded checksums locally (offline): `bash scripts/data/pubmed/verify_pubmed.sh all`; add `--repair` to re-fetch failures. Exit 1 iff a file fails; a missing `.md5` alone is a warning.

| Issue | Mitigation |
|---|---|
| Checksum mismatch after a download | `bash scripts/data/pubmed/verify_pubmed.sh all --repair` re-fetches only failed or checksum-less files |
| Early PMIDs have no abstract | Normal; short title-only records are `extract_status=partial` (docs/09) |
| Extract reports no input files | Nothing downloaded under `<raw>/pubmed/` yet, or `EPISTEME_RAW_ROOT` points elsewhere than where the download wrote |

#### PMC commercial open access (`pmc`)

Full-text JATS from the public AWS bucket `PMC_S3_BUCKET` (per-article objects with `metadata/PMC{id}.{version}.json`), licence-filtered to the commercial-friendly set. Raw: `<EPISTEME_RAW_ROOT>/pmc/oa_comm/` (`metadata/`, `xml/`, `filelists/`). `pmc` also owns `materialize` and `enrich`.

The article list comes from the bucket's daily S3 inventory (`inventory-reports/pmc-oa-opendata/metadata/<date>/manifest.json` and the `csv.gz` files it names; the bucket README section 5.2). It lists every article version (about 8 million). The downloader saves the inventory under `filelists/inventory/<date>/` (when the newest one cannot be read, it tries the next two older dates and deletes a damaged file), then checks each PMCID's versions from the highest down and keeps the first with a commercial licence. A version already on disk (metadata JSON and XML, neither empty) ends the check without a request, so a stopped run restarts cheaply and a newer version is still found. A PMCID with no commercial version goes into `filelists/noncommercial.txt` as `PMC{id}.{highest version}`; a re-run skips it until the inventory shows a newer version. Delete that file to check every PMCID again. A failed metadata request (anything but a 404) is a failure: the PMCID is not recorded, no older version is chosen in its place, and the run exits 1; run it again to retry. `--max-files N` stops after N downloads. NCBI ESearch is the fallback only when no inventory can be read, and it refuses a request for more than 9,999 ids (ESearch returns no id past that position); on that path and with a local `--filelist`, each PMCID's versions come from a listing of its `PMC{id}.` prefix.

```bash
# first time (bounded sample of 20 articles; the default cap is 10, 0 = everything)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pmc download --max-files 20
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pmc extract --max-files 20
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pmc load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pmc graph

# offline preview of the download plan (nothing resolved, nothing written)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pmc download --dry-run

# full pipeline including corpus materialization and OpenMetadata manifest (unbounded)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pmc all
```

Optional `NCBI_API_KEY` (section 1.2) for higher ESearch limits (fallback path only). `pmc enrich` writes the OpenMetadata manifest; ingest happens only if `OM_HOST` is set (otherwise the manifest file only).

| Issue | Mitigation |
|---|---|
| NCBI removed the legacy `oa_comm` / `oa_noncomm` prefixes, their filelists, the FTP `oa_bulk` packages and the baseline packages in the week of 2026-08-24 | The downloader uses the bucket inventory, the per-article layout and the metadata JSON |
| The inventory is regenerated daily, lags the bucket, and is kept for 30 days | A version listed but already removed is reported as `metadata not found` and does not fail the run |
| Full set is millions of articles (about 2 requests per article; 48 threads by default, each keeping its connection open) | Bound with `--max-files`; for a full pull (`--max-files 0`) use a long-lived session and re-run after a stop |
| Authors or journal missing | JSON alone is incomplete; the extractor reads the JATS XML |

#### Apollo corpus (`apollo`)

Hugging Face dataset `APOLLO_HF_REPO` (`FreedomIntelligence/ApolloCorpus`). Raw: `<raw>/apollo/`. Needs the Hugging Face CLI. The download ignores `--max-files` and `--force`; only extract honours `--max-files`.

```bash
# preview (offline; prints what it would download)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh apollo download --dry-run

# first time
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh apollo download
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh apollo extract --max-files 5
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh apollo load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh apollo graph
```

| Issue | Mitigation |
|---|---|
| Neither `hf` nor `huggingface-cli` found | `pip install -U "huggingface_hub[cli,hf_transfer]"`; the prerequisites table shows which is present |
| Sparse bibliographic fields | Expected for this corpus; docs/12 lists the licence class and the open commercial-use diligence |

#### Europe PMC preprints (`europepmc_preprint`)

Per-preprint full-text XML, harvested one id at a time from the Europe PMC REST API using the id list `pprid.txt.gz` at `EUROPEPMC_PREPRINT_BASE`. The old bulk range archives were discontinued by the upstream (spike of 2026-09-08). Raw: `<raw>/europepmc/preprints/PPR*.xml` plus a `.harvest_state` resume list. Ids are taken newest first so a capped run returns usable preprints. This is the smallest full literature chain; section 0.3 keeps it as the alternative fast path (it needs `ftp.ebi.ac.uk`).

```bash
# preview (fetches only the id list; prints "would harvest N ids"; writes nothing)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_preprint download --dry-run

# first time, bounded
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_preprint all --max-files 3

# stage by stage
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_preprint download --max-files 3
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_preprint extract --max-files 3
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_preprint load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_preprint graph

# incremental: same download command; already-harvested ids are skipped
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_preprint download
```

`--since` is accepted by this downloader but is a documented no-op (the id feed has no server-side filter). Per-id 404 (withdrawn, or no full text yet), 429 and 503 are counted in `errors=` and are not fatal; a 404 id is retried on the next run.

#### Europe PMC author manuscripts (`europepmc_manuscript`)

Author-accepted manuscript tarballs from `EUROPEPMC_MANUSCRIPT_BASE`; text-mining licensed, not for commercial mixing by default policy. Raw: `<raw>/europepmc/manuscripts/`. Positional modes on download: format `xml` (default) or `txt`; scope `all` (default), `baseline` or `incr`.

```bash
# first time, bounded: two incremental archives
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_manuscript download incr --max-files 2
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_manuscript extract --max-files 2
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_manuscript load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_manuscript graph

# baseline set (multi-GB tarballs; check free disk first)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_manuscript download baseline
```

| Issue | Mitigation |
|---|---|
| HTTP 503 from the Europe PMC edge | Transient; rerun later (the origin path is correct). The downloaders retry inside `aria2c`/`curl` |
| Very large baselines | Check free disk; interrupted runs resume, size-matched files are skipped |

#### Bookshelf (`bookshelf`)

NCBI LitArch open-access book packages; the wrapper reads the package list from `file_list.txt` (falling back to `file_list.csv`) under `BOOKSHELF_BASE` because the hashed tree has no usable directory listing. Raw: `<raw>/bookshelf/packages/`. Needs migration 0002 (bookshelf partitions, `article_parts`). A dry run can list thousands of urls.

```bash
# first time, bounded
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh bookshelf download --max-files 3
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh bookshelf extract --max-files 3
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh bookshelf load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh bookshelf graph
```

#### Guidelines (`guidelines`)

Hugging Face dataset `GUIDELINES_HF_REPO` (`epfl-llm/guidelines`); rows are `unknown` licence, subset `other` (docs/12). Raw: `<raw>/guidelines/`. Download ignores `--max-files`. The dataset is gated: accept its terms on the dataset page with your Hugging Face account, and put a read token in `.env` as `HF_TOKEN`.

```bash
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh guidelines download
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh guidelines extract --max-files 5
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh guidelines load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh guidelines graph
```

#### Europe PMC support feeds (no corpus rows)

**`europepmc_id_mappings`**: the PMID / PMCID / DOI table (`PMID_PMCID_DOI.csv.gz`, about 340 MB) from `EUROPEPMC_ID_MAPPINGS_BASE`; loaded into `episteme.id_map`, not `episteme.articles`. Raw: `<raw>/europepmc/id_mappings/`. Monthly upstream refresh.

```bash
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_id_mappings download
gzip -dc 01_raw/europepmc/id_mappings/PMID_PMCID_DOI.csv.gz | head -3
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_id_mappings load
```

Integrity check (mandatory before load): the `head` above must show the header `PMID,PMCID,DOI` and real rows. A payload holding error text (an earlier audit found Oracle `ORA-`/`SP2-` error text saved as the file) is a corrupt source: delete it and re-download. The load is idempotent (`ON CONFLICT DO NOTHING`), so a rerun of an unchanged file inserts no new rows; `--force` is accepted and does nothing.

**`europepmc_lite`**: `PMCLiteMetadata.tgz` (about 2 GB), weekly. It enriches existing `episteme.articles` rows (journal, year); it creates no rows, so load an article source first. Raw: `<raw>/europepmc/lite_metadata/`.

```bash
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_lite download
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_lite enrich
```

**`europepmc_abstracts`**: preprint abstract zips from `EUROPEPMC_ABSTRACTS_BASE`; download-only. The current, still-accumulating month is deferred (`abstracts: deferring current (unpublished) month`) unless `--include-current` is passed, because upstream lists the file before it is fetchable.

```bash
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_abstracts download
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh europepmc_abstracts download --include-current
```

| Issue | Mitigation |
|---|---|
| Interrupted 2 GB lite download | Rerun the same command; `aria2c -c` resumes (curl fallback also resumes) |
| Only the current month listed for abstracts | Deferred by design; retry next month, or pass `--include-current` deliberately |
| Corrupt id-mappings payload | Section above: `head` check, delete, re-download |

### 5.2 Structured databases (download, serialize, load)

Structured sources are serialized into declarative-prose rows in `episteme.articles`. `serialize` is the structured counterpart of `extract` and takes `--max-files N` and `--force`. `--max-files` on `serialize` bounds the number of input files parsed, so bound the download first (it decides which files exist). Licence overrides are recorded in `docs/12-source-inventory.md`: for `mesh`, `pubchem`, `clinvar` and `cdisc_ct` the serializer sets `public_domain` as an explicit, source-anchored governance override (decision 2026-09-19; extended to `cdisc_ct` 2026-09-28 — set in `ct_parse.build_rows`, called by `serialize_cdisc_ct.py`; `license_raw` keeps the upstream text; see docs/09 and docs/12). The `cdisc_bc` licence is UNVERIFIED and no serializer is planned until CDISC confirms it (section 5.5).

| Source | Download modes (positional) | Notes |
|---|---|---|
| `chembl` | `default` (SQLite + SDF + chemreps + ancillaries), `all` (adds postgresql/mysql/h5/fps dumps) | Files are taken in a fixed priority order (licence, readme, checksums, release notes, SQLite tarball, SDF ...) so a small `--max-files` gets ancillaries first |
| `uniprot` | none | Swiss-Prot only (no TrEMBL). Probes the mirrors in `UNIPROT_MIRRORS`. File order puts small ancillaries and the FASTA first, so `--max-files 4` is a usable FASTA-only set |
| `pubchem` | `compound_extras` (default), `rdf_compound`, `compound_full`, `all_nlp` | Directory listing under `PUBCHEM_BASE`; very large modes |
| `clinvar` | `tsv` (default), `vcf38`, `vcf37`, `xml`, `all` | Listing under `CLINVAR_BASE` |
| `reactome` | none | Flat current-release directory |
| `mesh` | optional 4-digit year (default current year, then previous) | Resolves `desc<year>.gz`; the only structured source with a graph stage (`mesh_hierarchy`, needs migration 0003) |
| `ontologies` | none | GO, HPO, MONDO (`.obo` and `.owl`) and UCUM from fixed URLs. UCUM is downloaded but not serialized |
| `openalex` | `works_jsonl` (default), `works_parquet`, `jsonl`, `parquet`, `full` | S3 prefix under `OPENALEX_S3`; needs the `aws` CLI (or `s5cmd` for an unbounded sync). An unknown mode makes the wrapper die (rc 2 directly, rc 1 through the dispatcher). Bounded fetch below |

First-time and incremental blocks (bounded where the option is real):

```bash
# uniprot: usable FASTA-only set, then serialize and load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh uniprot download --max-files 4
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh uniprot serialize --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh uniprot load

# chembl: ancillaries and release notes only (first 5 files of the priority list)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh chembl download --max-files 5

# pubchem, clinvar, reactome: one file each to try the wiring
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubchem download compound_extras --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh clinvar download tsv --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh reactome download --max-files 2

# mesh: full chain, small (one descriptor file); needs migration 0003 for graph
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh mesh all

# ontologies: first fixed item only (go.obo), then serialize and load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh ontologies download --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh ontologies serialize
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh ontologies load

# incremental for any structured source: rerun download, then serialize and load;
# to reprocess everything after an upstream release, force with a stated reason
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh chembl serialize --force --reason "reprocess after ChEMBL release"
```

`chembl serialize` and `chembl load` need the full SQLite release, not the bounded ancillary set above; the bounded `chembl` command only exercises the download wiring.

#### OpenAlex bounded fetch

`openalex` is the largest source (hundreds of GB unbounded). Bound it with `--max-files`:

```bash
# preview which first shard would be fetched (lists the whole S3 prefix, which can take minutes; writes nothing)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh openalex download --dry-run --max-files 1

# fetch the first works_jsonl shard, then serialize (biomedical subset) and load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh openalex download --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh openalex serialize --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh openalex load

# or the three stages in one go
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh openalex all --max-files 1
```

Semantics of `--max-files` for `openalex download`: empty or `0` runs the unlimited sync; a positive integer fetches that many shard files, oldest `updated_date=` partition first (byte order), skipping files already present at the right size, never counting `manifest.json`; a non-integer (`abc`, `-1`) dies with a message naming the value: exit 2 from the wrapper run directly, but rc 1 through `run_pipeline.sh` (`stage failed: download`). The serializer keeps only works tagged Medicine or Biology at the top concept level.

| Issue | Mitigation |
|---|---|
| `s3_fetch_first_n: awscli required` | Install the AWS CLI; a positive `--max-files` uses `aws`, an unbounded sync uses `s5cmd` if present, else `aws` |
| `no S3 tool` on an unbounded run | Install `s5cmd` or `awscli` |
| `uniprot: no mirror reachable` | All probed mirrors failed `reldate.txt`; check network or `UNIPROT_MIRRORS` |
| `mesh: ... none returned HTTP 200` | NLM layout changed; the wrapper warns and exits 0 with nothing resolved. Check `MESH_BASE` |
| Serializer prints `ERROR: no *.fasta.gz ... under <raw dir>` | Nothing downloaded yet, or the bounded set did not include the FASTA (uniprot needs `--max-files 4` or more) |

### 5.3 Volatile sources (download only)

These are download-only by design (Phase 1 retrieval material; docs/12). No serializer, no rows in `episteme.articles`. Outputs land in `<raw>/<source>/` with a `last_sync_utc.txt` stamp.

| Source | Modes | Notes |
|---|---|---|
| `dailymed` | `index`, `monthly`, `fullparts`, `all` (default) | Scrapes `.zip` hrefs from DailyMed SPL resource pages under `DAILYMED_BASE` |
| `openfda` | `label` (default), `drug`, `all_human`, `all` | Reads zip URLs out of the catalog JSON at `OPENFDA_CATALOG` |
| `aact` | none | Scrapes date-keyed daily snapshot links from `AACT_DOWNLOADS`; a page whose layout changed resolves 0 links and exits 0 without a stamp |

```bash
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh dailymed download index --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh openfda download label --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh aact download --max-files 1
```

| Issue | Mitigation |
|---|---|
| A single DailyMed page 404s | Warned and skipped; the rest of the modes continue |
| AACT `resolved 0 download links` | Page layout change; tracked as deferred follow-up, not a hard failure |

### 5.4 Acquisition mechanism (`hf_corpus`)

A generic Hugging Face fetch: `<repo_id>` is an argument, not an endpoint variable. Optional `[output_subdir] [revision]` follow it; `--repo-type dataset|model` selects the type (default `dataset`). Output: `<raw>/hf_corpus/<output_subdir>` (default: the repo id with `/` replaced by `_`), with `hf_repo_id.txt` and a stamp. `--max-files` and `--force` are ignored. Licence depends on the repo you pass and is not recorded by the pipeline.

```bash
# preview: with no repo id a dry run is a soft no-op (exit 0)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh hf_corpus download --dry-run

# fetch a repo (replace the placeholder; review its licence first)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh hf_corpus download <repo_id>
```

### 5.5 CDISC Biomedical Concepts (`cdisc_bc`)

CDISC Biomedical Concepts and SDTM dataset specialisations from the public GitHub repository `cdisc-org/COSMoS` (`export/` folder, CSV and XLSX). **Download-only; there is no serializer.** No modes; a stray positional token makes the wrapper die (rc 1 through the dispatcher).

- **Pinned to one commit.** Each run resolves the latest commit SHA of `main` first, then lists and fetches `export/` at that SHA, so one run is internally consistent.
- **`PROVENANCE.txt`** is written beside the data in `<raw>/cdisc_bc/`: source repo URL, `commit_sha`, `retrieved_at`, and the licence note. The repository `LICENSE` file is stored beside it and does not count against `--max-files`.
- **Licence: UNVERIFIED.** The repository `LICENSE` is MIT (repository code); the README grants CC-BY-4.0 to documentation and minutes only and states nothing for the `export/` data files. No serializer exists or is planned until CDISC confirms a licence for `export/` directly; this source is excluded from any training corpus in its current state. Verify before any redistribution (docs/12).
- **Re-fetch:** size-matched files are skipped and a same-size change under a new commit is not detected, so after an upstream update run with `--force --reason "..."`.
- Even `--dry-run` makes two GitHub API calls (commit and listing). Unauthenticated GitHub allows 60 requests per hour; hitting the limit fails the stage (rc 1 through the dispatcher) with `cannot resolve the latest commit ... (GitHub API rate limit?)`.

```bash
# bounded first run: two data files plus LICENSE and PROVENANCE.txt
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_bc download --max-files 2

# preview (network: GitHub API only; writes nothing)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_bc download --dry-run

# after an upstream commit, force a refetch
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_bc download --force --reason "refetch after COSMoS update"
```

Sample success (bounded first run): exit code 0. Without `aria2c` you see a WARN that it was not found and downloads fall back to sequential `curl`; that is expected. The run ends with `write_sync_stamp: ./01_raw/cdisc_bc/last_sync_utc.txt` and `pipeline done: cdisc_bc download`, and `<raw>/cdisc_bc/` then holds `LICENSE`, `PROVENANCE.txt`, `export/` and `last_sync_utc.txt`.

### 5.6 CDISC Controlled Terminology (`cdisc_ct`)

CDISC Controlled Terminology as published quarterly by NCI EVS: one tab-separated file per package (fixed order SDTM, SEND, ADaM, Define-XML, Protocol). `--max-files N` on `download` takes the first N packages of that fixed order; on `serialize` it takes the first N packages **present locally** (`discover_cdisc_ct_files` only ever considers packages actually found under `<raw>/cdisc_ct/`, in that same fixed order) — not a fixed set of five, since a prior bounded `download` may not have fetched every package. `download`, `serialize` and `load` are wired in the dispatcher (section 4.2); there is no `graph` stage. Downloads need `evs.nci.nih.gov` reachable — there is no offline path for this source.

- **Layout:** `<raw>/cdisc_ct/<Package>/<YYYY-MM-DD>/<Package>_Terminology.txt`, the date taken from the file's `Last-Modified` header (release dates differ per package — SDTM/SEND/ADaM/Define-XML and Protocol are not guaranteed to land on the same date). Each release folder also holds `PROVENANCE.txt` (source URL, `Last-Modified`, `retrieved_at`, NCI's licence statement) and `last_sync_utc.txt`. Older release folders are **never deleted** from disk (retire only removes database rows, below); they stay for provenance and a re-run does not re-fetch a size-matched file.
- **Content-Type gate, not HTTP status:** NCI's download site is a JavaScript app — a missing path answers **HTTP 200** with a small `text/html` fallback page, not a 404. The wrapper `HEAD`s each package's URL first and requires `Content-Type: text/plain`; anything else (including the HTML fallback), an unreachable host, or a downloaded file whose first line is not the exact 8-column header is a WARN naming the package, and the wrapper moves on leaving that package's raw tree untouched — **none of these set the run's exit code to failure**, only an actual fetch or move-into-place failure does (below). If `evs.nci.nih.gov` is unreachable, every package WARNs this way and the stage still exits 0 having fetched nothing; on a first run this only surfaces later, at `serialize` (`ERROR: no CDISC CT release files under <dir>`); on a quarterly re-run it is a silent no-op — `serialize` and `load` skip via their existing markers and `retire` keeps the old release. Treat an all-WARN download the same as `--dry-run` output on `europepmc_preprint` (section 0.3): a `0`/nothing-fetched result is a problem to check, not nothing to do.
- **Atomic download:** each file is fetched to `<Package>_Terminology.txt.part` in the release folder and only `mv`'d into place (as `<Package>_Terminology.txt`) after the header check passes; a stale `.part` is removed before a retry so `curl`'s resume can't append to it. A failed or discarded fetch leaves no partial file at the real path and removes an empty release/package directory it may have just created.
- **Serialize:** discovers, per package, only the **newest** `YYYY-MM-DD` release directory (older ones are ignored, never re-parsed). One row per codelist per package (large codelists split into parts of at most 200 terms, each part repeating the codelist header). `--report` prints a field-shape table without writing a shard, marker, manifest or audit row (section 6.3).
- **Licence:** `public_domain` governance override (decision 2026-09-28): NCI states CDISC Terminology is free to use without licensing restrictions; `license_raw` keeps that statement, `subset` resolves to `commercial`. Set in `ct_parse.build_rows`, not `serialize_cdisc_ct.py` (docs/09 section 6.3, docs/12).
- **Load (per package's newest shard only):** `load_cdisc_ct.sh` first runs `python -m episteme.data.cdisc_ct.retire --print-current-shards`, which prints one staging shard file name per package — the shard belonging to that package's newest **locally discovered and already-serialized** release. Those names are forwarded to `python -m episteme.data.load_articles --source cdisc_ct` as repeated `--only NAME`, so an older release's shard sitting in the same staging directory (left over from before the newest one was discovered/serialized) is never loaded, even under `--force` or after a retry — loading it would let the older release's stable ids briefly overwrite the newer release's rows. If no current shard is found for any package, nothing is loaded and the wrapper says so, then still runs retire.
- **Retire (per package, DB-verified):** only if the load step exits 0 does `load_cdisc_ct.sh` run `python -m episteme.data.cdisc_ct.retire`. Retire looks at every package it can currently find under the local raw tree and handles each **independently**:
  - a package present locally **and** whose newest release has already loaded (a `load_success` marker for its staging shard exists) is then further **verified against the database itself** before anything is deleted: (a) `select count(*) from episteme.articles where source='cdisc_ct' and source_file=<kept file>` must equal the row count recorded in that release's `load_success` marker (or be greater than zero, if the marker carries no usable count), and (b) the database must hold no row for that package dated **newer** than the kept release. Either check failing skips the package (prints a warning naming it, deletes nothing) — this guards against a local raw tree/marker that is stale or wrong relative to what is actually in the database. Once verified, the package's older releases' rows are deleted from `episteme.articles`/`article_body` (scoped to that package's own `source_file` prefix, so one package's retire can never touch another's rows), in one `load_replace` audit event per package;
  - a package present locally but whose newest release has **not** loaded yet: skipped for this run, with a printed reason — its old rows stay in place until the new ones are in;
  - a package **absent** from the local raw tree (for example after a `--max-files`-bounded download on this machine, or one that has never been fetched here) is **never touched** — its existing rows, if any, are left alone.

  A quarter therefore fully replaces the previous one per package, not as one all-or-nothing swap across all five packages.

```bash
# preview (HEAD requests only; writes nothing)
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct download --dry-run

# first time: fetch every package's current release, then serialize and load (which also retires)
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct download
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct serialize
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct load

# bounded first try: SDTM only (first package in fixed order)
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct download --max-files 1
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct serialize --max-files 1
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct load

# quarterly re-run once NCI publishes new releases: same commands: download lands the
# new dated folders (packages whose Last-Modified is unchanged are size-skipped),
# serialize picks up each package's newest release, and load's retire step then
# deletes each package's previous release once its new one has loaded
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct download
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct serialize
PGDATABASE=episteme_test EPISTEME_ACTOR=episteme_sys_admin bash scripts/data/run_pipeline.sh cdisc_ct load
```

What each prints: `download` logs one `cdisc_ct: <Package> release <date> fetched` line per package fetched (or `... up to date` when size-matched, or a WARN naming the reason for a skipped package — unreachable host, non-`text/plain` Content-Type, or unexpected header), then exits 0 unless a package's fetch or the final move into place actually failed, in which case it exits 1 after trying the remaining packages. A WARN-only run (nothing reachable) still exits 0 with nothing fetched. `serialize` prints `schema=... source=cdisc_ct`, then `done inputs=<n> ok=<n> failed=0 rows=<n>` (`inputs` is the number of packages whose newest release was discovered, capped by `--max-files`). `load` first prints, if there is nothing current to load, `cdisc_ct: no current (locally discovered + serialized) shards to load`; otherwise one `ok <shard> rows=... event=load_commit` line per package's newest shard only (an older shard in the same staging directory is never mentioned — it is neither loaded nor reported as a failure). Then the retire step's own lines: `<Package>: <basename> not loaded yet (no load_success marker) -- skipped` for a package it is not yet safe to retire, `<Package>: skipped -- <reason>` when the local raw tree/marker disagrees with the database itself (the DB row count for the kept release doesn't match its marker, or is zero; or the database holds a release for that package dated newer than the local newest), `<Package>: retired <n> article(s); kept <basename>` for a package it did retire, and a final `retired <total> article(s) across <n> package(s)` (or `no CDISC CT release files under <dir>; nothing to retire` when the local raw tree is empty, or `no loaded CDISC CT release to retire against; nothing retired` when every discovered package is still unloaded).

| Issue | Mitigation |
|---|---|
| A package logged as `not available (Content-Type '...')` | Normal for a package NCI has not published under that exact path right now; rerun later or check `CDISC_CT_BASE` in `scripts/data/_lib/sources.env` |
| `could not reach NCI for <Package>` on every package, download exits 0 | `evs.nci.nih.gov` is unreachable from this machine; nothing was fetched even though the stage reported success — check network access and retry before trusting a "done" download. On a quarterly run treat this the same way as `--dry-run`'s "would harvest 0 ids" (section 0.3): `0` fetched is a problem, not nothing to do |
| `<Package>: <basename> not loaded yet (no load_success marker) -- skipped` from retire | Its `load` step for that package's shard has not succeeded yet in this environment; rerun `load` once the shard is present under `<processed>/staging/cdisc_ct/` |
| `<Package>: skipped -- db row count ... marker recorded ...` or `... db holds a newer release ...` from retire | The database disagrees with the local raw tree/marker for that package (stale local state, a marker from a different environment, or a newer release loaded elsewhere) — re-download and re-serialize that package's current release before retrying, or investigate why the database's row count/newest date differs from what is local |
| `no CDISC CT release files under <dir>; nothing to retire` | Nothing has been downloaded and serialized into a shard yet for any package on this machine |
| Retire ran but an older release's rows are still there for one package | Check that package's newest release actually has a `load_success` marker (section 4.4) and that the database-verification checks above pass; a `--max-files`-bounded run only lands and loads the packages it capped to, so the others are correctly left alone |

### 5.7 CDISC USDM (`cdisc_usdm`)

CDISC's Unified Study Definitions Model from the public GitHub repository `cdisc-org/DDF-RA`: the `Deliverables/` folder of the latest release (OpenAPI spec, USDM controlled terminology, implementation guide PDF, CORE rules, UML model and data dictionary; about 15 MB for `v4.0.0`). **Download-only; there is no serializer.** No modes; a stray positional token makes the wrapper die (rc 1 through the dispatcher).

- **Pinned to one release.** Each run resolves the latest release tag, then that tag's commit SHA, then lists the whole tree at that SHA in one call; every file is fetched at the SHA. Files land in `<raw>/cdisc_usdm/<release-tag>/`, for example `<raw>/cdisc_usdm/v4.0.0/Deliverables/API/USDM_API.json`. A new release gets its own folder; older release folders are never touched.
- **`PROVENANCE.txt`** is written in the release folder: `source_repo`, `release_tag`, `commit_sha`, `retrieved_at`, and the licence and corpus-status notes. The repository `LICENSE` and `README.md` (which carries the licence wording) are stored beside it and do not count against `--max-files`.
- **Licence: UNVERIFIED.** The README grants MIT to code and scripts and CC-BY-4.0 to content files like documentation and minutes; the USDM model files are named in neither. No serializer exists or is planned until CDISC confirms a licence for the model files; this source is excluded from any training corpus. Verify before any redistribution (docs/12).
- **Re-fetch:** size-matched files are skipped, so a same-size change under a moved tag is not detected; run with `--force --reason "..."` in that case. If a release tag is moved or re-published, always use `--force --reason "..."` — otherwise a size-mismatched file is resumed onto the old local copy rather than replaced.
- Every run, `--dry-run` included, makes three GitHub API calls (release, commit, tree). Unauthenticated GitHub allows 60 requests per hour; hitting the limit fails the stage (rc 1 through the dispatcher) with `cannot resolve ... (GitHub API rate limit?)`.

```bash
# bounded first run: two data files plus LICENSE, README.md and PROVENANCE.txt
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_usdm download --max-files 2

# preview (network: GitHub API only; writes nothing)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_usdm download --dry-run

# force a refetch of the current release
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh cdisc_usdm download --force --reason "refetch USDM release"
```

Sample success (bounded first run): exit code 0. Without `aria2c` you see a WARN that it was not found and downloads fall back to sequential `curl`; that is expected. The run ends with `write_sync_stamp: ./01_raw/cdisc_usdm/last_sync_utc.txt` and `pipeline done: cdisc_usdm download`, and `<raw>/cdisc_usdm/<release-tag>/` then holds `Deliverables/`, `LICENSE`, `README.md` and `PROVENANCE.txt`.

---

## 6. Materialize, ingest and field-shape checks

### 6.1 `corpus materialize`

Writes the pretraining corpus shard under `<corpus>/` from `episteme.articles` in Postgres, with a `corpus_materialize` audit event. `corpus materialize` and `pmc materialize` run the same script (`scripts/data/materialize_corpus.sh`). No `--dry-run` (exit 3). It reads the database, so the guard applies. By default decontamination uses the mock question list; the real evaluation sets need `--no-sample-only` on the Python module and downloaded datasets.

```bash
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh corpus materialize
```

### 6.2 `load_articles`

`load` for every literature and structured source is `python -m episteme.data.load_articles --source <source>`, wrapped per source by `scripts/data/<source>/load_<source>.sh`. It reads every shard under `<processed>/staging/<source>/` (`*.parquet`, then `*.jsonl`), commits one transaction per shard, writes a `load_success` marker, and prints `ok <shard> rows=N deleted=N body_deleted=N event=...`, `skip <shard>` for an already-loaded shard, or `FAIL <shard>: <error>`. Exit 0 when nothing failed, 1 if any shard failed, 2 for `--force` without `--reason`. With no shards it prints `no shards under <dir>` and exits 0. Run inside `run_pipeline.sh` it does not add its own bracket (the dispatcher's run id is shared).

To load without the dispatcher (no `run_start`/`run_end` from the dispatcher; the loader adds its own):

```bash
PGDATABASE=episteme_test .venv/Scripts/python.exe -m episteme.data.load_articles --source europepmc_preprint
```

### 6.3 Field-shape `--report` (parse only, writes nothing)

Every extractor and serializer (`apollo`, `bookshelf`, `europepmc_manuscript`, `europepmc_preprint`, `guidelines`, `pubmed`, `chembl`, `uniprot`, `pubchem`, `clinvar`, `reactome`, `mesh`, `ontologies`, `openalex`, `cdisc_ct`) has a `--report` mode that parses in memory and prints a field-shape table with no shard, marker, manifest or audit row. The dispatcher does not forward `--report`; call the wrapper directly. On Windows set `PYTHONIOENCODING=utf-8` (known on Windows: the default `cp1252` console encoding has crashed this mode on non-ASCII text; the variable is a precaution and harmless elsewhere):

```bash
PYTHONIOENCODING=utf-8 bash scripts/data/uniprot/serialize_uniprot.sh --report --max-files 1
PYTHONIOENCODING=utf-8 bash scripts/data/pubmed/extract_pubmed.sh --report --max-files 1
```

Run it after each new download or parser change as the smoke check.

---

## 7. Verify

| Command | What it tells you |
|---|---|
| `bash scripts/data/verify_audit_trail.sh` | Recomputes the hash chain. Prints `audit chain OK` and exits 0, or `CHAIN BROKEN at seq [...]` and exits 1 (a `mirror_short` problem prints `[None]`). Needs `EPISTEME_ACTOR` and a reachable database (guarded like any Python connection); exit 2 if the actor is missing. Details and limits: docs/11 section 5. |
| `bash scripts/data/source_inventory.sh` | Read-only table `source last_sync rows` for every wired source. `last_sync` is the newest `last_sync_utc.txt` found anywhere under `<raw>/<source>/` (nested folders included); `rows` is the `episteme.articles` count per source. `n/a` means no stamp or no rows for that source, or (for `rows` on every line) the database is unreachable or refused by the guard. Does not need `EPISTEME_ACTOR`. |
| `bash scripts/data/pubmed/verify_pubmed.sh all` | Local MD5 check of the PubMed set (no network in plain mode). |
| `bash scripts/data/db/ensure_audit_partitions.sh [target_db] [months_ahead]` | Idempotent: creates the next `months_ahead` (default 6) monthly `episteme._audit_YYYYMM` partitions if missing, and re-applies `REVOKE UPDATE, DELETE ... FROM episteme_app` on every partition it touches (`ALTER DEFAULT PRIVILEGES` only grants, never revokes, on a newly created one). Not wired to a scheduler — run it periodically (e.g. monthly) as an operator step, well ahead of the partition boundary it protects; a month whose rows already landed in the default partition before its own partition existed cannot be split out after the fact. Needs `PGHOST`/`PGPORT`/`EPISTEME_SYS_ADMIN_PASSWORD`, not `EPISTEME_ACTOR`. Details: docs/11 section 5. |
| `bash scripts/data/rotate_audit_logs.sh [ops_dir]` | Gzips `_ops/_audit/audit-YYYYMMDD.jsonl` mirror files once they are about a month old (`find -mtime +30` is a floor comparison, so ~31 days in practice) in place, and best-effort marks today's still-open mirror file append-only (`chattr +a`) on Linux — a silent no-op elsewhere, including this Windows environment. The `chattr +a` step only ever targets `audit-<today's UTC date>.jsonl`; if run before that day's first audit write, the file does not exist yet and the `chattr` call silently no-ops (nothing gets marked append-only until a later run finds the file), so run it after the first audit write of the day, not before. `chattr` (both the pre-gzip `-a` clear and the end-of-run `+a`) needs root or `CAP_LINUX_IMMUTABLE` to actually take effect on Linux — it also no-ops (not an error) without that privilege; `gzip` itself needs neither, but a file a *previous, privileged* run actually marked `+a` can only be gzipped by a run that can also clear that flag (the same privilege) — run the script consistently as the same privileged user once `+a` has taken effect, or the unprivileged `-a` clear will keep silently failing and that file's `gzip` will WARN-and-skip every day. Not wired to a scheduler; safe to run daily from cron. `ops_dir` defaults to the real mirror directory (`episteme.audit_trail._mirror_dir()`, `EPISTEME_DATA_ROOT`-aware) when omitted. `verify_audit_trail.sh` counts both `.jsonl` and `.jsonl.gz` mirror files, so rotation does not break the hash-chain check. Needs no `EPISTEME_ACTOR` and no database. Details: docs/11 section 5. |

```bash
PGDATABASE=episteme_test bash scripts/data/verify_audit_trail.sh
PGDATABASE=episteme_test bash scripts/data/source_inventory.sh
```

Without the `PGDATABASE=episteme_test` prefix both fall back to the production name, are refused by the guard under the shipped `restricted` mode, and `source_inventory.sh` shows `n/a` for every row count.

---

## 8. Ops cadence

Upstream cadence per source is in `docs/12-source-inventory.md`. Suggested rhythm:

| When | Action |
|---|---|
| Daily | `pubmed download updates`, then `extract` and `load`; PMC delta if running continuous sync; author-manuscript incrementals (`europepmc_manuscript download incr`) when Europe PMC is up |
| Weekly | `europepmc_lite download` then `enrich`; review failed markers under `<processed>/_ops/<source>/` |
| Monthly | `europepmc_id_mappings download` (after the `head` integrity check) then `load`; `europepmc_abstracts download` |
| On upstream release | Rerun `download` for the structured sources; `serialize` and `load`; `cdisc_bc` with `--force --reason`; `cdisc_usdm` on a new USDM release (no `--force` needed: a new tag gets a new folder) |
| After each major pull | `--report` field-shape smoke check (section 6.3); `source_inventory.sh` |
| Before and after any production or go-live change | `verify_audit_trail.sh`; the docs/11 section 8 checklist |
| When SSD is ready | Full `pmc all`; consolidate `<raw>` |

### 8.1 CI and Dependabot

- **CI** (`.github/workflows/ci.yml`, check `tests`) runs on every pull request and every push to `main`: `uv sync --locked --extra dev`, `ruff format --check .`, `ruff check .`, and `pytest -m "not pg"` without `tests/test_run_pipeline_dispatch.py`. It includes the model smoke tests in `tests/model/`, which download a tiny Hugging Face model. The `pg` tests and the network dispatch smoke test stay local-only; run them before merging changes that touch the database or `run_pipeline.sh`.
- **`main` requires the `tests` check.** Every change reaches `main` through a pull request; direct pushes (including GitHub web-editor commits) are refused.
- **Dependabot** checks every Monday at 06:00 UTC for `uv` and GitHub Actions updates. Minor and patch updates arrive as one grouped pull request per ecosystem (`uv-minor-patch`, `gh-actions-minor-patch`), each major update as its own pull request, and security updates grouped as `security-fixes`. Commit messages start `chore(deps):` (uv) or `ci(deps):` (Actions). `.github/workflows/dependabot-auto-merge.yml` enables auto-merge on `security-fixes` pull requests only, and only while every commit on the branch is Dependabot's own, signed by GitHub (author `dependabot[bot]`, committer `web-flow`, verified signature); such a pull request merges by itself once `tests` passes. Version updates (grouped minor/patch and single majors) wait for a person.
- **A security pull request that did not merge** has a failed `tests` run, or a commit that is not Dependabot's (the workflow then turns auto-merge off and comments): open its Checks tab, fix the break on the Dependabot branch (or wait for a newer Dependabot push), and it merges when `tests` goes green.
- **Keep the required check.** Auto-merge relies on `main` requiring `tests`. Removing or renaming that check (in the ruleset or the workflow) turns auto-merge into an immediate merge of every Dependabot security pull request.
- **Commits added to a Dependabot branch stop auto-merge.** A commit that Dependabot did not make and sign turns auto-merge off on the next workflow run, and the pull request then needs a manual review and merge.

---

## 9. Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-08-31 | Initial runbook from the acquisition phase and sample audit |
| v2 | 2026-09-21 | SP5 rewrite: single operator runbook. Covers every wired source through `run_pipeline.sh`, the DB-mode guard, migrations 0002/0003 by hand, `episteme_test` targeting, `openalex` bounded fetch, `cdisc_bc`, verification. Dated status board replaced by `docs/12-source-inventory.md`; per-source facts checked against the wrappers |
