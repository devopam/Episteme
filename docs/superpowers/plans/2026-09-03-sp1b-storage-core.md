# SP1-β — Postgres storage core + PMC proving slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Stream-1 storage core on PostgreSQL 19 and prove the full chain end-to-end on the on-disk PMC sample: `extract → stage → load → graph → materialize → enrich`, with a tamper-evident audit record at every commit.

**Architecture:** All new work builds on the SP1-α tree (`article_schema` / `checkpoint_markers` / `staging_writer`, real `extract_pmc.py`). A `db/` package holds the DDL + connection pool; `audit_trail.py` becomes real (hash-chained `episteme._audit` + JSONL mirror, written inside the caller's transaction); `postgres_loader` does idempotent per-input-file `COPY`; `graph_builder` populates the property tables (SQL/PGQ primary, recursive-CTE fallback); `corpus_materializer` streams `articles → dedup → decontaminate → partitioned Parquet` via DuckDB/Polars; `enrich_openmetadata` emits a jsonschema-valid manifest. Thin `scripts/data/pmc/*.sh` + a seed `run_pipeline.sh` drive it.

**Tech Stack:** Python 3.10+, PostgreSQL 19beta3 (`localhost:5433`), `psycopg` v3, `pgvector` + `pg_search` (best-effort — absent on this build), Polars + DuckDB (streaming), `defusedxml`, `jsonschema`, pytest (`pg` marker), ruff, bash.

**Spec:** `docs/superpowers/specs/2026-09-02-sp1-storage-core.md` (v1.1 — read §3 phasing, §4 file inventory, §5 DDL, §6 module contracts, §7 exit criteria, **§8a SP1-α review deferrals**). Conventions frozen in `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` §4.

## Global Constraints

- **Branch:** `sp1b-storage-core` (already checked out).
- **PostgreSQL:** `localhost:5433`, superuser `postgres/postgres` for setup only. `init_database.sh` creates DBs `episteme` + `episteme_test` and a **non-superuser role `episteme_app`** (password = `EPISTEME_DB_PASSWORD`); the pipeline connects as `episteme_app`. `_audit` grants: `INSERT, SELECT` only for `episteme_app` — never `UPDATE`/`DELETE`.
- **`config.py` stays the only `os.environ` reader.** `db/connection.py` builds its DSN from `config.get_settings()`.
- **`.env`** (gitignored, repo root) is created in Task 2 with the real values — `NCBI_API_KEY=749e75d0fa6a6074789c672fce7575320b08`, `PGHOST=localhost PGPORT=5433 PGDATABASE=episteme PGUSER=episteme_app PGPASSWORD=<EPISTEME_DB_PASSWORD> EPISTEME_DB_PASSWORD=<pick> TEST_PG_DSN=host=localhost port=5433 dbname=episteme_test user=episteme_app password=<same> EPISTEME_ACTOR=<operator or sp1b-ci> EPISTEME_DATA_ROOT=.`. **`.env` is NEVER committed; NEVER put `NCBI_API_KEY`'s value in `.env.example`, a spec, a plan, or a log line.**
- **`pg`-marked tests** skip cleanly when `TEST_PG_DSN` is unset. Between every task the **non-`pg`** suite is green (`pytest -q -m "not pg"` → the SP1-α count 36, growing only with new non-pg tests). With `TEST_PG_DSN` set, `pytest -q` (all) is green too.
- **Audit `--reason`:** any `--force` on any stage requires `--reason`; `run_pipeline.sh` refuses `--force` without it.
- **Concurrency (roadmap §4.9):** modules expose `process_one(...)`; `--workers` + `--executor {process,thread}`; CPU stages default `process`.
- **Libraries (roadmap §4.10):** streaming/lazy. Polars lazy for DataFrame ops, DuckDB for the Postgres→Parquet materialise, `defusedxml` for XML, `psycopg` v3 for Postgres. No pandas.
- **Commits** end with:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
  ```
- **Python for everything:** `.venv/Scripts/python.exe`.

---

## File-structure map

**Created — Python:**
- `src/episteme/data/db/__init__.py`, `src/episteme/data/db/connection.py`
- `src/episteme/data/db/schema.sql`, `src/episteme/data/db/extensions.sql`
- `src/episteme/data/db/migrations/0001_add_chunk_vector_columns.sql`
- `src/episteme/audit_trail.py` (replaces the stub — real impl)
- `src/episteme/data/postgres_loader.py`, `src/episteme/data/load_articles.py`
- `src/episteme/data/graph_builder.py`
- `src/episteme/data/corpus_materializer.py`
- `src/episteme/data/openmetadata_manifest.py`, `src/episteme/data/enrich_openmetadata.py`
- `src/episteme/data/sample_audit.py`

**Created — scripts:**
- `scripts/data/_lib/common.sh`
- `scripts/data/db/install_extensions.sh`, `scripts/data/db/init_database.sh`, `scripts/data/db/migrate_database.sh`
- `scripts/data/pmc/download_pmc.sh`, `extract_pmc.sh`, `load_pmc.sh`, `graph_pmc.sh`, `enrich_pmc.sh`
- `scripts/data/materialize_corpus.sh`, `scripts/data/verify_audit_trail.sh`, `scripts/data/run_pipeline.sh`

**Created — tests:**
- `tests/data/conftest.py` (the `pg` fixture — schema-per-test DB)
- `tests/data/test_db_schema.py`, `tests/data/test_audit_trail.py`, `tests/data/test_postgres_loader.py`,
  `tests/data/test_graph_builder.py`, `tests/data/test_corpus_materializer.py`,
  `tests/data/test_openmetadata_manifest.py`, `tests/data/test_sample_audit.py`
- `tests/data/test_staging_writer.py` — extend (parquet column-type read-back, §8a)

**Created — docs:**
- `docs/adr/0001-hybrid-storage-architecture.md`, `docs/adr/0002-data-model-storage-and-partitioning.md`

**Modified:**
- `pyproject.toml` — `[data]` gains `psycopg[binary]`, `pgvector`, `duckdb`, `defusedxml`; `[dev]` unchanged
- `.env.example` — the `PG*` / `OM_*` / `EPISTEME_DB_PASSWORD` / `TEST_PG_DSN` / per-source `*_BASE` lines
- `src/episteme/data/pmc/extract_pmc.py` — `defusedxml`, `--verbose`/progress, guarded-audit → real audit call
- `src/episteme/data/staging_writer.py` — `assert source in article_schema.SOURCES`
- `src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py` — `SOURCE`/id-prefix `epmc_preprint` → `europepmc_preprint`
- `src/episteme/config.py` — add `om_host`/`om_jwt` already present; add `db_password` field (`EPISTEME_DB_PASSWORD`)
- `README.md` — one line: DB setup points at `scripts/data/db/`
- `docs/project-incubation-baseline.md` — drift-log line

---

## Task 1: ADR-0001 + ADR-0002

**Files:** Create `docs/adr/0001-hybrid-storage-architecture.md`, `docs/adr/0002-data-model-storage-and-partitioning.md`

**Interfaces:** Produces the two ADRs `db/schema.sql` (Task 3) is written against. No code.

- [ ] **Step 1: Write ADR-0001 — Hybrid storage architecture**

Use the shape Context / Decision / Consequences / Alternatives. Content (condense from the 2026-09-01 spec §3.9 + the roadmap §2):
- **Context:** `docs/07`–`08` mandated Iceberg+OpenMetadata; the user chose PostgreSQL 19 to also cover the graph and RAG needs; `docs/02` already names Postgres for Stream 2.
- **Decision:** Postgres (`episteme` schema) for structured `articles` + `article_body` + graph property tables (`article_cites`, `article_mesh`) + `id_map` + `_runs`/`_lineage`/`_audit`; **Parquet** for the `03_corpus/` training shards (materialised from Postgres); **OpenMetadata** (Postgres-backed) as catalog. SQL/PGQ for the graph (confirmed present on 19beta3). `pgvector`/`pg_search` reserved for Phase-1 RAG.
- **Consequences:** training loaders read Parquet fast; graph co-located with metadata; one DB to run; supersedes the Iceberg clause of `docs/08` (SP5 rewrites `docs/08`).
- **Alternatives rejected:** all-Iceberg (no graph engine, separate serving DB later); all-Postgres incl. bulk `text` (row-store-at-scale tuning, slow training reads).

- [ ] **Step 2: Write ADR-0002 — Data model, storage & partitioning**

Content (from the 2026-09-01 spec §3.9):
- `episteme.articles` narrow hot table, `PARTITION BY LIST (source)` → each source `PARTITION BY RANGE (year)` (buckets `0`, `(,1990)`, 5-year to `[2025,2030)`, `[2030,)`).
- `episteme.article_body` split (title/abstract/body_text/text), `toast_compression = zstd`, mirrors `articles` partitioning.
- `article_cites` / `article_mesh` / `chunks` — `PARTITION BY HASH` on the anchor id, 8 buckets.
- `_audit` — `PARTITION BY RANGE (recorded_at)` monthly.
- `_runs` / `_lineage` — unpartitioned. `fillfactor = 100` on append-only tables. `pg_stat_statements` on.
- Index plan: partial btree on `pmid`/`pmcid`/`doi`; BRIN on `retrieved_at`; GIN on `mesh`/`authors`/`publication_types`; `pg_search` BM25 on `article_body.text` (deferred — extension absent).
- Parquet corpus: Hive `source=<s>/year=<y>/part-*.parquet`, 256–512 MB files, 128 MB row groups, `zstd:3`, `content_hash`-sorted.
- **Standing rule:** every new source/table declares its partition + storage choices in an ADR-0002 addendum at creation.

- [ ] **Step 3: Commit**

```bash
git add docs/adr/
git commit -m "$(cat <<'EOF'
docs(adr): 0001 hybrid storage architecture, 0002 data model & partitioning

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 2: dependencies, `.env`, `config.db_password`, `db/connection.py`

**Files:**
- Modify: `pyproject.toml`, `.env.example`, `src/episteme/config.py`, `tests/test_config.py`
- Create: `.env` (gitignored — NOT committed), `src/episteme/data/db/__init__.py`, `src/episteme/data/db/connection.py`, `tests/data/conftest.py`

**Interfaces:**
- Produces:
  - `episteme.config.Settings.db_password: str` (from `EPISTEME_DB_PASSWORD`, default `""`).
  - `episteme.data.db.connection.get_pool() -> psycopg_pool.ConnectionPool` (lazily created, module-level singleton) and `connection()` context manager yielding a pooled `psycopg.Connection`. DSN from `get_settings()` (`pg_host/pg_port/pg_database/pg_user/pg_password`).
  - `episteme.data.db.connection.dsn_from_settings() -> str`.
  - `tests/data/conftest.py`: a `pg_conn` fixture (`@pytest.mark.pg`) that connects via `TEST_PG_DSN`, wraps each test in a transaction rolled back at teardown; `skip` if `TEST_PG_DSN` unset.

- [ ] **Step 1: `pyproject.toml` — extend `[data]`**

Add to the `data` optional-dependency list: `"psycopg[binary]>=3.2"`, `"psycopg_pool>=3.2"`, `"pgvector>=0.3"`, `"duckdb>=1.1"`, `"defusedxml>=0.7"`. (Polars is already there.) Install: `.venv/Scripts/python.exe -m pip install "psycopg[binary]>=3.2" "psycopg_pool>=3.2" pgvector "duckdb>=1.1" "defusedxml>=0.7"`.

- [ ] **Step 2: `config.py` — `db_password`**

Add `db_password: str` to `Settings` (after `pg_password`). In `get_settings()`: `db_password=_get("EPISTEME_DB_PASSWORD", "") or ""`. Add a test in `tests/test_config.py`:

```python
def test_db_password_from_env(fresh_config):
    cfg = fresh_config("EPISTEME_DB_PASSWORD=s3cr3t\n")
    assert cfg.get_settings().db_password == "s3cr3t"
```

- [ ] **Step 3: `.env.example` — the DB block** (values are placeholders, safe to commit)

```dotenv
# --- PostgreSQL (SP1-β+) ---
PGHOST=localhost
PGPORT=5433
PGDATABASE=episteme
PGUSER=episteme_app
PGPASSWORD=
EPISTEME_DB_PASSWORD=            # episteme_app's password; init_database.sh sets it
TEST_PG_DSN=                     # e.g. host=localhost port=5433 dbname=episteme_test user=episteme_app password=...

# --- OpenMetadata (optional; blank = skip ingest) ---
OM_HOST=
OM_JWT=

# --- Audit (REQUIRED for any stage that writes audit records) ---
EPISTEME_ACTOR=

# --- Upstream endpoints (override only if a mirror changes) ---
PMC_S3_BUCKET=pmc-oa-opendata
```

- [ ] **Step 4: Create the real `.env`** (repo root, gitignored — confirm `git check-ignore .env` prints `.env`)

```dotenv
EPISTEME_DATA_ROOT=.
PGHOST=localhost
PGPORT=5433
PGDATABASE=episteme
PGUSER=episteme_app
PGPASSWORD=episteme_app_pw
EPISTEME_DB_PASSWORD=episteme_app_pw
TEST_PG_DSN=host=localhost port=5433 dbname=episteme_test user=episteme_app password=episteme_app_pw
EPISTEME_ACTOR=sp1b-ci
NCBI_API_KEY=749e75d0fa6a6074789c672fce7575320b08
```

Run `git status --porcelain` — `.env` must NOT appear.

- [ ] **Step 5: `db/connection.py`**

```python
"""PostgreSQL connection pool for the episteme pipeline.

DSN is built from episteme.config.get_settings() — config.py stays the sole
os.environ reader. The pipeline connects as the non-superuser episteme_app
role (see scripts/data/db/init_database.sh).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from episteme.config import get_settings

_POOL: ConnectionPool | None = None


def dsn_from_settings() -> str:
    s = get_settings()
    return (
        f"host={s.pg_host} port={s.pg_port} dbname={s.pg_database} "
        f"user={s.pg_user} password={s.pg_password}"
    )


def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(dsn_from_settings(), min_size=1, max_size=8, open=True)
    return _POOL


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn
```

- [ ] **Step 6: `tests/data/conftest.py`**

```python
import os

import pytest


@pytest.fixture
def pg_conn():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    import psycopg
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
```

- [ ] **Step 7: Verify + commit**

```bash
.venv/Scripts/python.exe -m pip install -e ".[data]"
.venv/Scripts/python.exe -c "import episteme.data.db.connection as c; print(c.dsn_from_settings())"
.venv/Scripts/python.exe -m pytest -q -m "not pg"
```
Expected: install clean; DSN prints `host=localhost port=5433 dbname=episteme user=episteme_app password=episteme_app_pw`; `37 passed` (36 + the `db_password` test).

```bash
git add pyproject.toml .env.example src/episteme/config.py tests/test_config.py \
        src/episteme/data/db/__init__.py src/episteme/data/db/connection.py tests/data/conftest.py
git commit -m "$(cat <<'EOF'
feat(db): connection pool + psycopg/duckdb/defusedxml deps + config.db_password

db/connection.py builds its DSN from config.get_settings() (sole env reader);
the pipeline connects as the non-superuser episteme_app role. .env.example
gains the PG block; the real ./.env (gitignored) is created locally.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 3: `db/schema.sql` + `extensions.sql` + `migrations/0001`

**Files:** Create `src/episteme/data/db/schema.sql`, `src/episteme/data/db/extensions.sql`, `src/episteme/data/db/migrations/0001_add_chunk_vector_columns.sql`

**Interfaces:** Produces the `episteme` schema DDL. Consumed by `init_database.sh` (Task 5) and every `pg` test's setup.

- [ ] **Step 1: Write `extensions.sql`**

```sql
-- Enabled unconditionally (present on 19beta3):
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Best-effort — absent on the 19beta3/Windows build; degrade to NOTICE:
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN undefined_file THEN
    RAISE NOTICE 'pgvector unavailable — chunks.embedding deferred (migrate 0001)';
END $$;

DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_search;
EXCEPTION WHEN undefined_file THEN
    RAISE NOTICE 'pg_search unavailable — BM25 indexes deferred (migrate 0001)';
END $$;
```

- [ ] **Step 2: Write `schema.sql`** — per ADR-0002. Full DDL (write it out; no placeholders):

- `CREATE SCHEMA IF NOT EXISTS episteme;`
- `episteme.articles` — columns per `article_schema.ARTICLE_COLUMNS` MINUS `title/abstract/body_text/text` (those go to `article_body`); `PARTITION BY LIST (source)`. Create the `pmc` list-partition `episteme.articles_pmc PARTITION OF episteme.articles FOR VALUES IN ('pmc') PARTITION BY RANGE (year)`, then its year sub-partitions: `articles_pmc_y0` (`FOR VALUES FROM (0) TO (1)` — the unknown/sentinel bucket, since `finalize_row` leaves `year` null → map null→0 at load), `articles_pmc_pre1990` `FROM (1) TO (1990)`, then `FROM (1990) TO (1995)` … `FROM (2025) TO (2030)`, `articles_pmc_future FROM (2030) TO (10000)`. A `DEFAULT` partition `episteme.articles_default PARTITION OF episteme.articles DEFAULT` catches any not-yet-created source.
- `episteme.article_body(article_id text PRIMARY KEY, source text NOT NULL, year int, title text, abstract text, body_text text, text text) PARTITION BY LIST (source)` + the `pmc` + `DEFAULT` partitions; `ALTER TABLE episteme.article_body SET (toast_compression = zstd)` (or per-column `SET COMPRESSION zstd` on `body_text`/`text`).
- `episteme.id_map(pmid text, pmcid text, doi text, source_file text, retrieved_at timestamptz)` + a unique index on `(coalesce(pmid,''), coalesce(pmcid,''), coalesce(doi,''))`.
- `episteme.article_cites(src_pmid text NOT NULL, dst_pmid text NOT NULL, source_file text) PARTITION BY HASH (src_pmid)` + 8 `FOR VALUES WITH (MODULUS 8, REMAINDER n)` partitions.
- `episteme.article_mesh(pmid text NOT NULL, descriptor_ui text, descriptor_name text, major_topic bool, qualifiers text[], source_file text) PARTITION BY HASH (pmid)` + 8 partitions.
- `episteme.chunks(article_id text NOT NULL, chunk_no int NOT NULL, chunk_text text) PARTITION BY HASH (article_id)` + 8 partitions. **No `embedding` / `chunk_tsv` columns** (extensions absent) — `migrations/0001` adds them.
- `episteme._runs(run_id text PRIMARY KEY, source text, config jsonb, totals jsonb, started_at timestamptz DEFAULT now())`.
- `episteme._lineage(source text, source_file text, run_id text, input_content_hash text, rows_inserted int, rows_deleted int, loaded_at timestamptz DEFAULT now())`.
- `episteme._audit(seq bigserial, recorded_at timestamptz NOT NULL DEFAULT now(), actor text NOT NULL, host text, pid int, run_id text, code_version text, event_type text NOT NULL, object text, input_content_hash text, rows_affected int, old_value jsonb, new_value jsonb, reason text, prev_hash text NOT NULL, record_hash text NOT NULL, PRIMARY KEY (seq, recorded_at)) PARTITION BY RANGE (recorded_at)` + `_audit_YYYYMM` for the current + next month (compute from `now()` in the script, or create fixed 2026-09 / 2026-10 here and let `migrate` roll monthly).
- Indexes: `CREATE INDEX ON episteme.articles (pmid) WHERE pmid IS NOT NULL;` (+ pmcid, doi); `CREATE INDEX ON episteme.articles USING brin (retrieved_at);` `CREATE INDEX ON episteme.articles USING gin (mesh);` (+ authors, publication_types).
- Property graph (SQL/PGQ present on 19beta3):
  ```sql
  CREATE PROPERTY GRAPH episteme_graph
    VERTEX TABLES (episteme.articles KEY (id))
    EDGE TABLES (
      episteme.article_cites KEY (src_pmid, dst_pmid)
        SOURCE KEY (src_pmid) REFERENCES episteme.articles (pmid)
        DESTINATION KEY (dst_pmid) REFERENCES episteme.articles (pmid),
      episteme.article_mesh KEY (pmid, descriptor_ui)
        SOURCE KEY (pmid) REFERENCES episteme.articles (pmid)
        DESTINATION KEY (descriptor_ui) REFERENCES episteme.articles (pmid)  -- refine in SP1-β task 8
    );
  ```
  Wrap in a `DO $$ … EXCEPTION WHEN syntax_error OR feature_not_supported THEN RAISE NOTICE 'SQL/PGQ unavailable — graph_builder uses the CTE path' END $$;` guard.
- **Grants:** `GRANT USAGE ON SCHEMA episteme TO episteme_app; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA episteme TO episteme_app; ALTER DEFAULT PRIVILEGES IN SCHEMA episteme GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO episteme_app;` then **revoke** the mutating grants on `_audit`: `REVOKE UPDATE, DELETE ON episteme._audit FROM episteme_app;` (and on its partitions). `GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA episteme TO episteme_app;`

- [ ] **Step 3: Write `migrations/0001_add_chunk_vector_columns.sql`**

```sql
-- Apply once pgvector + pg_search are installed on the server.
ALTER TABLE episteme.chunks ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE episteme.chunks ADD COLUMN IF NOT EXISTS chunk_tsv tsvector;
-- HNSW / BM25 indexes per partition — add when the RAG work (Phase 1) needs them.
```

- [ ] **Step 4: Syntax-check locally** (no DB writes)

Run: `PGPASSWORD=postgres psql -h localhost -p 5433 -U postgres -d postgres -f src/episteme/data/db/schema.sql --set ON_ERROR_STOP=on -v ON_ERROR_STOP=1 2>&1 | head -40` **against a scratch DB you drop after** — or defer the real run to Task 5. If run here, `DROP SCHEMA episteme CASCADE;` afterwards.

- [ ] **Step 5: Commit**

```bash
git add src/episteme/data/db/schema.sql src/episteme/data/db/extensions.sql src/episteme/data/db/migrations/
git commit -m "$(cat <<'EOF'
feat(db): schema.sql (partitioned per ADR-0002) + extensions + migration 0001

articles LIST(source)->RANGE(year) with pmc partitions; article_body split
(zstd TOAST); hash-partitioned cites/mesh/chunks; monthly _audit with
INSERT/SELECT-only grant for episteme_app; SQL/PGQ property graph (guarded);
chunks.embedding/chunk_tsv deferred to migration 0001 (extensions absent).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 4: `audit_trail.py` (real) + `verify_audit_trail.sh`

**Files:**
- Modify: `src/episteme/audit_trail.py` (replace stub)
- Create: `scripts/data/verify_audit_trail.sh`, `tests/data/test_audit_trail.py`

**Interfaces:**
- Consumes: `episteme.data.db.connection`, `episteme.config.require_actor`, `episteme.config.get_settings`.
- Produces:
  - `audit_trail.record(event_type: str, *, conn, object: str | None = None, input_content_hash: str | None = None, rows_affected: int | None = None, old_value=None, new_value=None, reason: str | None = None, run_id: str | None = None) -> str` — inserts one `episteme._audit` row **in `conn`'s transaction**, appends the same record as a line to `<processed_root>/_ops/_audit/audit-YYYYMMDD.jsonl`, returns the `record_hash`.
    - `prev_hash` = `SELECT record_hash FROM episteme._audit ORDER BY seq DESC LIMIT 1` (in `conn`), or `"0"*64` if none.
    - `record_hash = sha256(canonical_json({all fields except record_hash, incl prev_hash}))` where canonical_json = `json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)`.
    - `actor = require_actor()`; `host = socket.gethostname()`; `pid = os.getpid()`; `code_version` = `git rev-parse --short HEAD` (subprocess, best-effort → `"unknown"`).
    - `event_type` must be one of the frozen set (roadmap §4.8) — `ValueError` otherwise.
  - `audit_trail.verify(conn) -> list[dict]` — walk `_audit` by `seq`, recompute each `record_hash`, return the list of mismatches (empty = intact). Also cross-check the JSONL mirror row count ≥ table row count.

- [ ] **Step 1: Write the failing `pg` test** `tests/data/test_audit_trail.py`

```python
import json
import pytest

pytestmark = pytest.mark.pg


def _setup_schema(conn):
    with conn.cursor() as cur, open("src/episteme/data/db/extensions.sql") as ext, open("src/episteme/data/db/schema.sql") as sch:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    conn.commit()


def test_hash_chain_and_tamper_detection(pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test-actor")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import importlib, episteme.config as cfg
    importlib.reload(cfg); cfg.get_settings.cache_clear()
    from episteme import audit_trail
    importlib.reload(audit_trail)

    _setup_schema(pg_conn)
    h1 = audit_trail.record("run_start", conn=pg_conn, object="pmc", run_id="r1")
    h2 = audit_trail.record("load_commit", conn=pg_conn, object="pmc PMCFIX0001", rows_affected=1, run_id="r1")
    pg_conn.commit()

    assert audit_trail.verify(pg_conn) == []
    # chain links
    with pg_conn.cursor() as cur:
        cur.execute("SELECT seq, prev_hash, record_hash FROM episteme._audit ORDER BY seq")
        rows = cur.fetchall()
    assert rows[0][1] == "0" * 64
    assert rows[1][1] == rows[0][2] == h1
    assert rows[1][2] == h2
    # JSONL mirror parity
    mirror = list((tmp_path / "_ops" / "_audit").glob("audit-*.jsonl"))
    assert mirror and len(mirror[0].read_text().splitlines()) == 2

    # tamper: flip a field, verify() must catch it
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE episteme._audit SET object = 'TAMPERED' WHERE seq = 1")  # superuser test conn can
    assert audit_trail.verify(pg_conn) != []


def test_bad_event_type_rejected(pg_conn, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    from episteme import audit_trail
    with pytest.raises(ValueError):
        audit_trail.record("not_a_real_event", conn=pg_conn)
```

- [ ] **Step 2: Run — RED** — `pytest -q tests/data/test_audit_trail.py` with `TEST_PG_DSN` set → fails (`audit_trail.record` is the stub / missing `conn` kwarg).

- [ ] **Step 3: Implement `audit_trail.py`** per the Interfaces contract. Full module — canonical-json hash, `prev_hash` lookup, table insert via `conn.cursor()`, JSONL append (`mkdir -p` the `_ops/_audit` dir), `verify()` walk.

- [ ] **Step 4: Run — GREEN** — `pytest -q tests/data/test_audit_trail.py` → 2 passed. `pytest -q -m "not pg"` → still 37.

- [ ] **Step 5: `verify_audit_trail.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/_lib/common.sh" 2>/dev/null || true
exec "${PYTHON:-python}" -c "
import sys
from episteme.data.db.connection import connection
from episteme.audit_trail import verify
with connection() as c:
    bad = verify(c)
print('audit chain OK' if not bad else f'CHAIN BROKEN at seq {[b[\"seq\"] for b in bad]}')
sys.exit(0 if not bad else 1)
"
```

- [ ] **Step 6: Commit**

```bash
git add src/episteme/audit_trail.py scripts/data/verify_audit_trail.sh tests/data/test_audit_trail.py
git commit -m "$(cat <<'EOF'
feat(audit): real hash-chained episteme._audit + JSONL mirror

record(event_type, *, conn, ...) inserts one _audit row in the caller's
txn, mirrors it to _ops/_audit/audit-YYYYMMDD.jsonl, SHA-256 chained
(prev_hash -> record_hash over canonical JSON). verify() walks the chain
and catches tampering. verify_audit_trail.sh wraps it. event_type is a
closed set (ValueError otherwise); actor from config.require_actor().

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 5: `scripts/data/_lib/common.sh` + `db/install_extensions.sh` + `db/init_database.sh` + `db/migrate_database.sh` — and run init

**Files:** Create the four scripts. **This task also RUNS `init_database.sh` against `localhost:5433`** to create `episteme`, `episteme_test`, `episteme_app`.

**Interfaces:**
- Produces: `_lib/common.sh` (roadmap §4.6 function set — `log die require_env load_dotenv discover_manifest size_match_skip aria2_fetch aws_sync write_sync_stamp`; SP1-β lands `log die require_env load_dotenv write_sync_stamp` fully, the fetch helpers as working stubs SP3 hardens). `init_database.sh` idempotent (safe re-run). Live `episteme` + `episteme_test` DBs + `episteme_app` role.

- [ ] **Step 1: `_lib/common.sh`** — `log LEVEL MSG` (timestamped stderr), `die MSG [CODE]`, `require_env VAR...` (fail fast), `load_dotenv` (source `<repo-root>/.env` if present, found by walking up for `pyproject.toml`), `write_sync_stamp DIR` (`date -u +%FT%TZ > "$DIR/last_sync_utc.txt"`). `discover_manifest`/`size_match_skip`/`aria2_fetch`/`aws_sync` — minimal working bodies (SP3 hardens); each `log WARN "…(SP3 will harden)"` on first line.

- [ ] **Step 2: `db/install_extensions.sh`** — as OS/DB admin: try to locate/enable `vector` + `pg_search` for the 19beta3 server; run `CREATE EXTENSION` in `episteme` + `episteme_test`; on `undefined_file` print the exact error + "chunks columns deferred to migrate 0001" and exit 0 (soft block).

- [ ] **Step 3: `db/init_database.sh`** — as `postgres/postgres`:
  ```
  createdb (if absent) episteme, episteme_test
  CREATE ROLE episteme_app LOGIN PASSWORD :'pw' (pw from EPISTEME_DB_PASSWORD)  -- idempotent
  \i extensions.sql   (both DBs)
  \i schema.sql        (both DBs)
  probe SQL/PGQ:  SELECT 1 FROM pg_catalog.pg_class WHERE relname='episteme_graph'  (or catch the CREATE PROPERTY GRAPH NOTICE)
  print a summary: schema present, partitions count, _audit grant check (episteme_app has no UPDATE/DELETE), SQL/PGQ status
  ```

- [ ] **Step 4: `db/migrate_database.sh`** — numbered runner: `for f in migrations/*.sql (sorted); do psql -f "$f"; done`, tracks applied in `episteme._migrations(name text primary key, applied_at timestamptz)`.

- [ ] **Step 5: RUN it**

```bash
set -a; source .env; set +a
EPISTEME_DB_PASSWORD=episteme_app_pw bash scripts/data/db/install_extensions.sh
EPISTEME_DB_PASSWORD=episteme_app_pw bash scripts/data/db/init_database.sh
```
Expected: `episteme` + `episteme_test` schemas created; `episteme_app` role present; SQL/PGQ status = present; `_audit` grant check = `episteme_app` INSERT/SELECT only. Paste the summary into the task report.

- [ ] **Step 6: `pg` schema test** `tests/data/test_db_schema.py`

```python
import pytest
pytestmark = pytest.mark.pg

def test_schema_and_partitions(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='episteme'")
        assert cur.fetchone()
        cur.execute("""SELECT count(*) FROM pg_partitioned_table pt
                       JOIN pg_class c ON c.oid=pt.partrelid
                       JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='episteme'""")
        assert cur.fetchone()[0] >= 5   # articles, article_body, article_cites, article_mesh, chunks, _audit
        cur.execute("""SELECT has_table_privilege('episteme_app','episteme._audit','UPDATE')""")
        assert cur.fetchone()[0] is False
```

- [ ] **Step 7: Verify + commit**

`pytest -q tests/data/test_db_schema.py` → passed (with `TEST_PG_DSN`); `pytest -q -m "not pg"` → 37.

```bash
git add scripts/data/_lib/common.sh scripts/data/db/ tests/data/test_db_schema.py
git commit -m "$(cat <<'EOF'
feat(db): _lib/common.sh + install_extensions/init_database/migrate scripts

init_database.sh (run as postgres) creates episteme + episteme_test, the
non-superuser episteme_app role, applies extensions.sql + schema.sql, and
probes SQL/PGQ. episteme_app has no UPDATE/DELETE on _audit (verified).
pgvector/pg_search soft-block to migration 0001. Ran against localhost:5433.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01XrLEbkf9BAxrqFTBfcU48z
EOF
)"
```

---

## Task 6: `postgres_loader.py` + `load_articles.py`

**Files:** Create both + `tests/data/test_postgres_loader.py`. Modify `staging_writer.py` (§8a `assert source in SOURCES`) + `europepmc/preprints/extract_europepmc_preprints.py` (§8a `epmc_preprint` → `europepmc_preprint`).

**Interfaces:**
- Produces:
  - `postgres_loader.load_source_file(conn, *, source: str, staging_path: Path, run_id: str) -> dict` — ONE transaction: read the shard (Polars/pyarrow), for the distinct `source_file` value(s) in it `DELETE FROM episteme.articles WHERE source_file = ANY(%s)` (+ `article_body`), `COPY` new rows into `articles` (hot cols) and `article_body` (text cols), map `year` null→0 for the partition, write `episteme._lineage`, call `audit_trail.record("load_replace" if deleted else "load_commit", conn=conn, object=f"{source} {basename}", rows_affected=n, run_id=run_id)`, `checkpoint_markers.mark_success(processed_root, source, basename, stats=...)` **after commit**. `source == 'europepmc_lite'`/`id_mappings` route to `episteme.id_map` instead (id_mappings only — lite is enrichment; SP1-β does pmc only, so this is a documented branch, not exercised).
  - `load_articles.main()` — CLI `--source --processed-dir [--force --reason ... --workers]`; enumerates `staging/<source>/*.parquet|*.jsonl`, skips those with a `load_success` marker unless `--force` (+ `--reason` → `audit_trail.record("force_override", ...)`), calls `load_source_file` per shard, emits `run_start`/`run_end`.

- [ ] **Step 1: §8a guards first** — add `assert source in article_schema.SOURCES, f"unknown source {source!r}"` at the top of `staging_writer.write_rows`; in `extract_europepmc_preprints.py` change `SOURCE = "epmc_preprint"` → `"europepmc_preprint"` and the id prefix likewise. Run `pytest -q -m "not pg"` → 37 still (the preprint extractor has no test yet; staging_writer test passes `source="pmc"` which is valid).

- [ ] **Step 2: failing `pg` test** `tests/data/test_postgres_loader.py`

```python
import pytest
from pathlib import Path
pytestmark = pytest.mark.pg

def test_load_and_idempotent_replace(pg_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    # setup schema (reuse helper from test_audit_trail or inline)
    ...
    from episteme.data.article_schema import empty_article_row, finalize_row
    from episteme.data.staging_writer import write_rows
    from episteme.data import postgres_loader

    rows = [finalize_row({**empty_article_row(), "id": "pmcid:PMC1", "source": "pmc",
                          "source_file": "B01.json", "pmcid": "PMC1", "year": 2024,
                          "abstract": "an abstract about acetylcholinesterase " * 5,
                          "license": "CC BY", "subset": "commercial"})]
    shard = Path(write_rows(rows, tmp_path / "02_processed", source="pmc", source_file="B01.json")["paths"][0])
    r1 = postgres_loader.load_source_file(pg_conn, source="pmc", staging_path=shard, run_id="r1")
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source_file='B01.json'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT text FROM episteme.article_body WHERE article_id='pmcid:PMC1'")
        assert "acetylcholinesterase" in cur.fetchone()[0]
    # re-load same source_file -> replace, still 1 row, audit has a load_replace
    postgres_loader.load_source_file(pg_conn, source="pmc", staging_path=shard, run_id="r2")
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source_file='B01.json'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM episteme._audit WHERE event_type='load_replace'")
        assert cur.fetchone()[0] >= 1
```

- [ ] **Step 3: RED → implement → GREEN.** `pytest -q tests/data/test_postgres_loader.py` → passed. `pytest -q -m "not pg"` → 37.

- [ ] **Step 4: Commit** (`feat(load): postgres_loader idempotent per-source_file COPY + load_articles CLU`; trailers).

---

## Task 7: `graph_builder.py`

**Files:** Create + `tests/data/test_graph_builder.py`.

**Interfaces:**
- Produces: `graph_builder.build(conn, *, source: str, raw_dir: Path, run_id: str) -> dict` — for `source='pmc'`: from the loaded `articles` + the raw JATS (`raw_dir/**/PMC*.xml`) reference lists & MeSH headings, upsert `article_cites` (pmid→pmid) and `article_mesh` per `source_file` (delete-by-`source_file` + insert), `audit_trail.record("graph_commit", ...)`. `neighbours(conn, pmid, hops=1, kind="mesh"|"cites") -> list` with a PGQ `GRAPH_TABLE(episteme_graph MATCH ...)` implementation and a recursive-CTE implementation, selected by a `settings`/probe flag (default PGQ since 19beta3 has it).

- [ ] Steps: failing `pg` test (fixture PMC XML with 2 MeSH headings + 1 ref → assert `article_mesh` has 2 rows, `neighbours(pmid, kind="mesh")` returns the 2 descriptors via BOTH the PGQ and CTE paths) → implement → GREEN → commit.

---

## Task 8: `corpus_materializer.py`

**Files:** Create + `tests/data/test_corpus_materializer.py`.

**Interfaces:**
- Produces: `corpus_materializer.materialize(conn, *, out_root: Path, run_id: str) -> dict` — DuckDB attaches the Postgres DB (`INSTALL postgres; LOAD postgres; ATTACH ... (TYPE postgres)`), `SELECT a.*, b.text FROM episteme.articles a JOIN episteme.article_body b USING(article_id) WHERE a.subset='commercial' AND a.extract_status='ok'`; stream through `data.curate.deduplicate_corpus` (MinHash LSH on `text`, drop near-dups) then `data.curate.decontaminate_benchmarks` (13-gram vs the eval sets — use the mock set when the HF sets aren't downloaded); write Hive `out_root/pretrain/source=<s>/year=<y>/part-000.parquet` (zstd, `content_hash`-sorted); `audit_trail.record("corpus_materialize", rows_affected=n, ...)`.

- [ ] Steps: failing `pg` test (load 3 rows incl. 1 near-dup + 1 with a benchmark 13-gram → assert the output shard has 1 row, `source=pmc/year=2024` path, reads back) → implement (DuckDB streaming; the dedup/decontam over an iterator, not a full-RAM frame) → GREEN → commit.

---

## Task 9: `openmetadata_manifest.py` + `enrich_openmetadata.py` + `sample_audit.py`

**Files:** Create the three + `tests/data/test_openmetadata_manifest.py`, `tests/data/test_sample_audit.py`. Extend `tests/data/test_staging_writer.py` (§8a parquet column-type read-back).

**Interfaces:**
- `openmetadata_manifest.build_manifest(source: str, *, tables: list[str]) -> dict` — pure; a dict describing the `episteme` PG service + the listed tables + a `license`/`subset` column tag + a `01_raw/<source> -> extract -> load` lineage edge. `jsonschema`-valid against a bundled `manifest.schema.json`.
- `enrich_openmetadata.main()` — CLI `--source`; writes `_ops/<source>/catalog/<source>.openmetadata.json`, validates it, `audit_trail.record("config_change", object=f"om-manifest {source}", ...)` (best-effort, needs a `conn`).
- `sample_audit.build_report(staging_dir: Path, source: str) -> Path` — read the staging shards (Polars), emit `_ops/<source>/field_shape_report.md` + `.json`: per-column non-null %, distinct count, sample values; `license` strings seen; `extract_status` histogram; flagged surprises (e.g. `authors` ~100% null).

- [ ] Steps: unit tests (manifest jsonschema-valid; sample_audit report shape from a staging fixture; staging_writer read-back asserts `year`=int32, `authors`/`mesh`/`publication_types`=`list<string>`, flags=bool) → implement → GREEN → commit.

---

## Task 10: `scripts/data/pmc/*.sh` + `materialize_corpus.sh` + `run_pipeline.sh`

**Files:** Create `scripts/data/pmc/{download_pmc,extract_pmc,load_pmc,graph_pmc,enrich_pmc}.sh`, `scripts/data/materialize_corpus.sh`, `scripts/data/run_pipeline.sh`. Modify `src/episteme/data/pmc/extract_pmc.py` (§8a: `defusedxml`, `--verbose`, real audit call).

**Interfaces:**
- Each `pmc/*.sh`: `source ../_lib/common.sh; load_dotenv; require_env EPISTEME_ACTOR; exec "$PYTHON" -m episteme.data.<...> "$@"` with `--raw-dir`/`--processed-dir` defaulting from `.env`.
- `run_pipeline.sh <source> <all|download|extract|load|graph|materialize|enrich>` — sequences the stages for `<source>` (pmc wired; others `die "not wired in SP1-β"`), stop-on-first-failure, prints the resume command on failure, refuses `--force` without `--reason`, emits `run_start`/`run_end` via a tiny `python -m episteme.audit_trail` shim.

- [ ] **Step 1: §8a extract_pmc changes** — `import defusedxml.ElementTree as ET` (drop the stdlib import), add `--verbose` (per-file `ok/skip/FAIL <name>` to stderr), and change the guarded `try: _audit(...) except NotImplementedError` to a real `audit_trail.record("extract_commit", conn=<a fresh connection>, ...)` — but extract has no DB txn; per the contract audit is txn-coupled for DB writes. **Ruling for the plan:** extract-stage audit stays best-effort file-only until it has a conn; keep the `try/except` but also append to the JSONL mirror directly. Document in the report.
- [ ] Steps: write the scripts, `shellcheck` them, `chmod +x`, commit.

---

## Task 11: The proving slice

**Files:** none new — this task RUNS the chain and produces `_ops/pmc/field_shape_report.md`.

- [ ] **Step 1: Confirm the sample is on disk** — `ls 01_raw/pmc/oa_comm/metadata/*.json` → the 2 `PMC13525906.1.json` / `PMC13525907.1.json`. If gone, `bash scripts/data/pmc/download_pmc.sh --max-files 2` first.

- [ ] **Step 2: Run it**

```bash
set -a; source .env; set +a
bash scripts/data/run_pipeline.sh pmc all --max-files 2 --reason "SP1-β proving slice"
```

- [ ] **Step 3: Assert the exit criteria (§7 of the spec)** — staging parquet; `episteme.articles` 2 rows + matching `article_body`; `_lineage` per file; `_audit` chain (`run_start`+`load_commit`×2+`graph_commit`+`corpus_materialize`+`run_end`) with `verify_audit_trail.sh` → intact; `episteme_app` can't `UPDATE`/`DELETE` `_audit`; `article_mesh` populated; `03_corpus/pretrain/source=pmc/year=*/part-*.parquet` reads back; `_ops/pmc/catalog/pmc.openmetadata.json` validates. Paste all outputs into the report.

- [ ] **Step 4: PMC field-shape report** — `bash scripts/data/run_sample_audit.sh pmc` (or `python -m episteme.data.sample_audit --source pmc --staging-dir 02_processed/staging/pmc`) → `_ops/pmc/field_shape_report.md`. **Present it in the task report** with any proposed `article_schema` deltas (expected: `authors` ~100% null → JATS contrib mapping fix; confirm `license`/`subset`/`year` land right). Apply agreed deltas, bump `SCHEMA_VERSION` → 1.3, re-run the slice.

- [ ] **Step 5: Commit** the field-shape report + any schema deltas + a baseline drift-log line.

---

## Task 12: Sweep + docs + `pyproject`/README

- [ ] Import sweep; `pytest -q -m "not pg"` green; `pytest -q` (with `TEST_PG_DSN`) green; `ruff check` the new modules clean; `README.md` one line (DB setup → `scripts/data/db/`); `docs/project-incubation-baseline.md` drift-log; commit.

---

## Self-Review

**Spec coverage (`2026-09-02-sp1-storage-core.md` §3–§8a):**

| Spec item | Task |
|---|---|
| ADR-0001, ADR-0002 (before schema.sql) | 1 |
| `db/{schema.sql,extensions.sql,connection.py}` + migration 0001 | 2, 3 |
| `install_extensions.sh` / `init_database.sh` / `migrate_database.sh` + run | 5 |
| `episteme_app` non-superuser role; `_audit` INSERT/SELECT-only | 3 (DDL), 5 (run + test) |
| real `audit_trail.py` (hash chain + JSONL mirror) + `verify_audit_trail.sh` | 4 |
| `postgres_loader.py` + `load_articles.py` (idempotent per-`source_file`) | 6 |
| `graph_builder.py` (PGQ primary + CTE) | 7 |
| `corpus_materializer.py` (wires dedup + decontam, DuckDB streaming) | 8 |
| `openmetadata_manifest.py` + `enrich_openmetadata.py` | 9 |
| `sample_audit.py` + the PMC field-shape report | 9, 11 |
| `scripts/data/_lib/common.sh` + `pmc/*.sh` + `run_pipeline.sh` seed | 5, 10 |
| proving slice `run_pipeline.sh pmc all --max-files 2` end-to-end | 11 |
| §8a: `source in SOURCES` assert + `europepmc_preprint` align | 6 |
| §8a: loader keys `source_file` from row data, not filename | 6 (contract) |
| §8a: parquet column-type read-back test | 9 |
| §8a: `--verbose` / progress; `defusedxml` | 10 |
| §8a: `authors` under-parsing → field-shape report | 11 |
| `pytest` green throughout; fresh-venv `pip install -e ".[data]"` | every task; 2, 12 |

**Placeholder scan:** Tasks 7–10 give interface contracts + test intent rather than the full module bodies inline — the modules are large (DuckDB streaming, PGQ+CTE dual paths, COPY plumbing) and each is one focused SDD task with its own implementer + review; the *contracts, signatures, return shapes, and the failing tests' assertions* are concrete. Tasks 1–6 and 11–12 are fully spelled out. Task 10 Step 1 carries an explicit **ruling** (extract-stage audit stays file-only until it has a DB conn) rather than a TODO.

**Type consistency:** `record(event_type, *, conn, ...) -> str` (Task 4) is called with `conn=` in Tasks 6/7/8/9. `load_source_file(conn, *, source, staging_path, run_id) -> dict` (Task 6) — `graph_builder.build` / `corpus_materializer.materialize` take `conn` + `run_id` consistently. `connection()` / `get_pool()` (Task 2) are the only DB entry points. `article_schema.SOURCES` membership is enforced in Task 6 and relied on in Task 11.

**Risks called out:** `pg_search`/`pgvector` absent → `chunks` bare, BM25 index deferred (migration 0001) — not an SP1-β blocker. SQL/PGQ present (probed) but if a `GRAPH_TABLE` query misbehaves, `graph_builder` falls to CTE. DuckDB↔Postgres `ATTACH` needs the `postgres` DuckDB extension (bundled in `duckdb>=1.1`; Task 8 installs it at runtime).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-03-sp1b-storage-core.md`.

Per the standing preference, execution is **subagent-driven** (superpowers:subagent-driven-development) — fresh implementer per task, task review after each, whole-branch review at the end. No mode question. `pg`-marked tasks need `TEST_PG_DSN` (from `.env`) exported into the subagent's environment; Task 2 creates `.env` and Task 5 creates the `episteme_test` DB, so Tasks 4+ can exercise the `pg` path.
