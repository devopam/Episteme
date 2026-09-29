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
3. **Shell:** Git Bash on Windows (or any bash). Run from the repository root; the default roots are relative paths (`./01_raw`, `./02_processed`, `./03_corpus`) and the `pmc` downloader writes to `./01_raw/pmc/oa_comm` relative to the current directory.
4. **Never print `.env`.** It holds credentials. Section 1.3 shows how to check that a key is set without showing its value.
5. **Do not run a bulk download unbounded** unless that is the actual task. Use `--max-files N` (section 4.3) and read section 5 for which sources honour it.

### 0.3 Fast path: smallest real end-to-end run

Source: `openalex`, one works shard, through download, serialize and load (the structured chain; `graph` is not wired for `openalex`, section 4.2). It needs the `aws` CLI (section 1.1) and internet access to the public OpenAlex S3 bucket (`OPENALEX_S3` in `scripts/data/_lib/sources.env`); it does not touch `ftp.ebi.ac.uk`. An alternative literature chain on Europe PMC is at the end of this section.

Prerequisites: section 1.1 tools (including `aws`), `.env` (section 1), a reachable `episteme_test`, and migrations 0002 and 0003 applied. Run the check in section 2.3 first and expect `t|t|t`; if it does not, apply the migrations there before step 3. `EPISTEME_ACTOR` is free text recorded on audit rows; for a test run `episteme_sys_admin` is what the repository's own tests use (the value is only recorded as text on audit rows, so any attributable name works). Because each Bash invocation is a fresh shell (section 0.2 rule 1), the commands below carry `EPISTEME_ACTOR=episteme_sys_admin` as an inline prefix, like `PGDATABASE`; a real environment variable beats `.env`, so the prefix always wins. If the preflight in step 1 prints `EPISTEME_ACTOR=MISSING`, nothing else is needed: the prefix supplies it. Without any value the dispatcher exits 2.

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

Exit codes of `run_pipeline.sh`: 0 success; 1 a stage failed (the message is `stage failed: <name>` and `resume with: ...`); 2 usage error, missing `EPISTEME_ACTOR`, or `--force` without `--reason`; 3 unknown source, or a source and stage combination that is not wired (section 4.2). Failures that originate inside a stage wrapper (a missing endpoint key such as `OPENALEX_S3` or `COSMOS_API_BASE`, a bad `--max-files`, an unknown mode) exit 2 or 1 when the wrapper is run directly, but through `run_pipeline.sh` they surface as rc 1 (`stage failed: <name>`). On Windows, Python stages may also print pool shutdown noise on stderr (`couldn't stop thread 'pool-1-...'` or a `hint: you can try to call 'close()'...` line; observed with `run_pipeline.sh`, `verify_audit_trail.sh` and `source_inventory.sh`, see section 0.3); they were not seen to affect the exit code.

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

### 1.2 Keys (names only)

Values are never shown in this document. Put them in `.env` (gitignored) or the real environment.

| Key | Meaning |
|---|---|
| `EPISTEME_ACTOR` | **Required** by `run_pipeline.sh` and every stage wrapper (the dispatcher and the wrappers exit 2 if unset). Identity recorded on every audit row (free text; for a test run `episteme_sys_admin` is acceptable). `source_inventory.sh` does not need it; `db/*.sh` do not use it. |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` | Connection used by the Python stages (role `episteme_app` at runtime). `PGDATABASE` is what the guard checks (section 3). |
| `EPISTEME_DB_PASSWORD` | Password `init_database.sh` sets for `episteme_app`. |
| `EPISTEME_SYS_ADMIN_PASSWORD` | Password of `episteme_sys_admin`, the DDL role used by `db/*.sh` and the migration commands below. |
| `POSTGRES_SUPERUSER`, `POSTGRES_SUPERUSER_PASSWORD` | Bootstrap role for `install_extensions.sh` and `init_database.sh` (operator only; see section 2). |
| `EPISTEME_DATA_ROOT` | Base of the three roots below (default `.`). |
| `EPISTEME_RAW_ROOT`, `EPISTEME_PROCESSED_ROOT`, `EPISTEME_CORPUS_ROOT` | Default `<data root>/01_raw`, `<data root>/02_processed`, `<data root>/03_corpus`. |
| `EPISTEME_DB_MODE`, `EPISTEME_DB_TARGET`, `PGDATABASE_SECONDARY`, `EPISTEME_PRODUCTION_DATABASE` | The database-mode guard (section 3). |
| `NCBI_API_KEY` | Optional. Raises NCBI rate limits for the PMC downloader. |
| `EPISTEME_DOWNLOAD_THREADS` | Parallel aria2c jobs (default 4). |
| `EPISTEME_SAMPLE_LIMIT` | Default for the extract/serialize `--max-files` when the flag is absent (default 0 = all files). |
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

### 2.3 Migrations: 0001 blocked, 0002 and 0003 applied by hand

Migrations live in `src/episteme/data/db/migrations/`. `scripts/data/db/migrate_database.sh [target_db]` applies them in order and records each in `episteme._migrations`, but it stops at the first failure:

| Migration | Adds | Status on the local build |
|---|---|---|
| `0001_add_chunk_vector_columns.sql` | `chunks.embedding` (pgvector) and `chunks.chunk_tsv` | **Cannot apply:** `vector` and `pg_search` are absent on PG19beta3. Needed only for Phase 1 retrieval. |
| `0002_container_and_book_parts.sql` | `articles.container_id` and `book_meta`, the `bookshelf` partitions, `article_parts` | Required before **any** load (the loader writes those columns) and before `graph`. |
| `0003_mesh_hierarchy.sql` | `episteme.mesh_hierarchy` | Required before `mesh` graph. |

Because `migrate_database.sh` dies at 0001 and never reaches 0002 or 0003, apply those two directly. Both are idempotent (safe to re-run); applied this way they are **not tracked** in `episteme._migrations` (see the header of each file and docs/08 section 9). Use `episteme_sys_admin`, never `episteme_app`:

```bash
PGDATABASE=episteme_test PSQL="/c/Program Files/PostgreSQL/19/bin/psql" bash -c '. scripts/data/_lib/common.sh; load_dotenv 2>/dev/null; for f in 0002_container_and_book_parts 0003_mesh_hierarchy; do PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "${PSQL:-psql}" -X -v ON_ERROR_STOP=1 -q -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$PGDATABASE" -f "src/episteme/data/db/migrations/$f.sql" || exit 1; done; echo migrations-applied'
```

Expect `migrations-applied` and exit 0. Check the result (expect `t|t|t`):

```bash
PGDATABASE=episteme_test PSQL="/c/Program Files/PostgreSQL/19/bin/psql" bash -c '. scripts/data/_lib/common.sh; load_dotenv 2>/dev/null; PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "${PSQL:-psql}" -X -At -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$PGDATABASE" -c "select to_regclass(\$\$episteme.article_parts\$\$) is not null, to_regclass(\$\$episteme.mesh_hierarchy\$\$) is not null, exists(select 1 from information_schema.columns where table_schema=\$\$episteme\$\$ and table_name=\$\$articles\$\$ and column_name=\$\$container_id\$\$)"'
```

On a server where pgvector is installed, `bash scripts/data/db/migrate_database.sh episteme_test` applies all three and tracks them. Do not run it on the local build.

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
bash scripts/data/run_pipeline.sh <source> <stage> [--dry-run] [--force] [--reason REASON] [--max-files N]
bash scripts/data/run_pipeline.sh corpus materialize
```

Stages: `all`, `download`, `extract`, `load`, `graph`, `materialize`, `enrich`, `serialize`. Sources are the keys of the `WRAPPER` table in `scripts/data/run_pipeline.sh` (all listed in `docs/12-source-inventory.md`), plus the alias `corpus` (materialize only).

Order of checks: usage (no source or stage: exit 2), then `EPISTEME_ACTOR` (exit 2), then argument validation (`--force` without `--reason`: exit 2), then an unknown source (exit 3; these dispatcher checks are the only place rc 2 and 3 come from; a wrapper's own failures, including a missing endpoint key, surface as rc 1), an unrecognised stage word (usage, exit 2) and an unwired combination (exit 3), then the audit bracket and the stage. All validation happens before any audit row, so a rejected command leaves no dangling `run_start`.

Tokens the dispatcher does not recognise are not dropped: they are forwarded to the download wrapper as the MODE or repo argument (for example `parquet` for `openalex`, `all` for `chembl`, a repo id for `hf_corpus`) and to flags such as `--include-current` or `--since`. Only the download wrapper sees them.

### 4.2 Stage matrix

| Source class | download | extract | serialize | load | graph | enrich | materialize | all |
|---|---|---|---|---|---|---|---|---|
| `pmc` | yes | yes | no (exit 3) | yes | yes | yes | yes | download, extract, load, graph, materialize, enrich |
| Literature: `pubmed`, `apollo`, `europepmc_preprint`, `europepmc_manuscript`, `guidelines`, `bookshelf` | yes | yes | no | yes | yes | no | no | download, extract, load, graph |
| Structured: `chembl`, `uniprot`, `pubchem`, `clinvar`, `reactome`, `mesh`, `ontologies`, `openalex`, `cdisc_ct` | yes | no | yes | yes | `mesh` only | no | no | download, serialize, load (plus graph for `mesh`) |
| `europepmc_id_mappings` | yes | no | no | yes (to `episteme.id_map`) | no | no | no | download only |
| `europepmc_lite` | yes | no | no | no | no | yes | no | download only |
| `europepmc_abstracts`, `hf_corpus`, `dailymed`, `openfda`, `aact`, `cdisc_bc` | yes | no | no | no | no | no | no | download only |
| `corpus` (alias) | no | no | no | no | no | no | yes | not allowed |

Any combination not marked yes exits 3 with a message such as `chembl extract is not in SP2 - SP4 (structured serialize)`. Row counts and table effects: `docs/09-extraction-contract.md`.

### 4.3 Flags

- **`--dry-run`** is supported on `download` only; any other stage (including `all`) exits 3 with `... writes the DB - --dry-run is supported on 'download' only`. A dry run writes no files and skips the whole audit bracket (`dry-run: skipping run_start/run_end audit bracket`). It is **not** always offline: most wrappers still list or probe the upstream over the network (`curl` listings, mirror and release probes; `cdisc_bc` makes two GitHub API calls that count against the unauthenticated 60 requests per hour limit). The Hugging Face wrappers (`apollo`, `guidelines`, `hf_corpus`) only echo what they would do. `hf_corpus` with no repo id is a soft no-op (exit 0) under `--dry-run` and an error otherwise.
- **`--max-files N`** bounds the run; the meaning is per stage:
  - `download`: caps the resolved file set (`pmc` translates it to `--limit`; default `--limit 10` when absent, `0` means all). Honoured by every source except `apollo`, `guidelines` and `hf_corpus` (the Hugging Face CLI resumes by itself; the flag is ignored with an INFO line).
  - `extract` and `serialize`: caps the number of input files parsed (default from `EPISTEME_SAMPLE_LIMIT`, `0` = all).
  - `all`: applies to its download, extract and serialize stages.
  - `load`, `graph`, `enrich`, `materialize`: ignored.
  - `openalex` download, specifically: empty or `0` means unlimited (the whole `s3 sync`); a positive integer fetches the first N shard files in byte order (oldest `updated_date=` partitions first, `manifest.json` excluded); a non-integer such as `abc` or `-1` makes the wrapper die with its own exit code 2 when run directly; through `run_pipeline.sh` the observable rc is 1 (`stage failed: download`, `resume with: ...`).
  - `pubmed`: each data file is two entries (the `.xml.gz` and its `.md5`), so `--max-files 2` is one data file with its checksum.
- **`--force --reason "<why>"`** re-does work that already has a success marker: extract and serialize reprocess, load reloads shards (the `load_articles` path records a `force_override` audit row with the reason), download wrappers delete and re-fetch the capped set. `--force` without a non-empty `--reason` exits 2 (`--force requires --reason`). Not every wrapper acts on `--force` (the Hugging Face wrappers and `europepmc_preprint` say so and ignore it). Forcing a reload is an audited exception: state a real reason.

### 4.4 Audit bracket and resume

Every non-dry run records `run_start` and, on completion, `run_end`, both with object = the source and a run id `<source>-<UTC timestamp>` shared by all stages of the invocation. On a stage failure the dispatcher records a `run_end` with reason `failed at stage <name>`, prints `resume with: run_pipeline.sh <source> <name>` and exits 1. A failed `run_start` or `run_end` (database down, refused by the guard) is only a `WARN`, so a run can complete without its bracket; verify (section 7). Extract and serialize stages keep a per-file success marker and skip finished files; `load` keeps a per-shard `load_success` marker. Re-running a stage is therefore the resume mechanism.

### 4.5 Per-stage arguments (what the dispatcher forwards)

| Stage | Receives |
|---|---|
| `download` | `--max-files N`, `--force --reason R`, tokens it does not recognise; `--dry-run` as `EPISTEME_DRY_RUN=1` |
| `extract`, `serialize` | `--max-files N`, `--force` only (never `--reason`) |
| `load` | `--force --reason R` only |
| `graph`, `enrich`, `materialize` | nothing |

---

## 5. Sources

Each block gives what it is, where it lands, the first-time commands, the incremental commands, and known failures. Licence and cadence per source: `docs/12-source-inventory.md`. All commands carry `PGDATABASE=episteme_test`; downloads need internet access. "Incremental" means: rerun the same commands. Downloaders skip size-matched files; extract, serialize and load skip files that already have a success marker.

### 5.1 Literature (download, extract, load, graph)

Row contract and status rules: `docs/09-extraction-contract.md`. Load and graph need migration 0002 (section 2.3).

#### PubMed (`pubmed`)

Citation and abstract XML: baseline (annual) and daily update files, from `PUBMED_FTP_BASE`. Not full text. Raw: `<raw>/pubmed/baseline/` (mode `baseline`, the default), `<raw>/pubmed/updates/` (mode `updates`, the upstream `updatefiles` directory) and the published checksums under `<raw>/pubmed/md5/`.

```bash
# first time (bounded): one baseline file plus its checksum
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed download --max-files 2
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed extract --max-files 1
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed load
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed graph

# whole baseline in one go (large; unbounded)
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed all

# incremental: daily update files
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed download updates
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed extract
PGDATABASE=episteme_test bash scripts/data/run_pipeline.sh pubmed load
```

Verify downloaded checksums locally (offline): `bash scripts/data/pubmed/verify_pubmed.sh all`; add `--repair` to re-fetch failures. Exit 1 iff a file fails; a missing `.md5` alone is a warning.

| Issue | Mitigation |
|---|---|
| Checksum mismatch after a download | `bash scripts/data/pubmed/verify_pubmed.sh all --repair` re-fetches only failed or checksum-less files |
| Early PMIDs have no abstract | Normal; short title-only records are `extract_status=partial` (docs/09) |
| Extract reports no input files | Nothing downloaded under `<raw>/pubmed/` yet, or `EPISTEME_RAW_ROOT` points elsewhere than where the download wrote |

#### PMC commercial open access (`pmc`)

Full-text JATS from the public AWS bucket `PMC_S3_BUCKET` (per-article objects with `metadata/PMC{id}.{version}.json`), licence-filtered to the commercial-friendly set. Raw: `./01_raw/pmc/oa_comm/` **relative to the current directory** (the downloader's default output directory is not read from `EPISTEME_RAW_ROOT`; run from the repository root and keep `EPISTEME_RAW_ROOT=./01_raw`, or the extractor will not find the files). `pmc` also owns `materialize` and `enrich`.

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

Optional `NCBI_API_KEY` (section 1.2) for higher ESearch limits. `pmc enrich` writes the OpenMetadata manifest; ingest happens only if `OM_HOST` is set (otherwise the manifest file only).

| Issue | Mitigation |
|---|---|
| Legacy `oa_comm/xml/all/` bulk prefixes are empty since August 2026 | The downloader uses the per-article layout plus metadata JSON |
| Full set is millions of articles | Bound with `--max-files`; use SSD and a long-lived session for a full pull |
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

Hugging Face dataset `GUIDELINES_HF_REPO` (`epfl-llm/guidelines`); rows are `unknown` licence, subset `other` (docs/12). Raw: `<raw>/guidelines/`. Download ignores `--max-files`.

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
| On upstream release | Rerun `download` for the structured sources; `serialize` and `load`; `cdisc_bc` with `--force --reason` |
| After each major pull | `--report` field-shape smoke check (section 6.3); `source_inventory.sh` |
| Before and after any production or go-live change | `verify_audit_trail.sh`; the docs/11 section 8 checklist |
| When SSD is ready | Full `pmc all`; consolidate `<raw>` |

---

## 9. Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-08-31 | Initial runbook from the acquisition phase and sample audit |
| v2 | 2026-09-21 | SP5 rewrite: single operator runbook. Covers every wired source through `run_pipeline.sh`, the DB-mode guard, migrations 0002/0003 by hand, `episteme_test` targeting, `openalex` bounded fetch, `cdisc_bc`, verification. Dated status board replaced by `docs/12-source-inventory.md`; per-source facts checked against the wrappers |
