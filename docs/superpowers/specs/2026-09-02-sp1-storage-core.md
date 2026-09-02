# SP1 — Storage core + PMC proving slice

**Date:** 2026-09-02
**Status:** Draft — awaiting operator review + PostgreSQL 19 environment
**Parent:** `docs/superpowers/specs/2026-09-02-phase0-data-roadmap.md` (SP1 charter, §3; frozen conventions, §4)
**Branch:** `spec/sp1-storage-core` (spec) → implementation branch per writing-plans

This spec fleshes out roadmap SP1. It does **not** restate the roadmap's frozen conventions
(§4: `scripts/data/` layout, `staging_writer` / `checkpoint_markers` / `audit_trail` /
`_lib/common.sh` / CLI / `.env` / `SOURCES` / idempotency) — those bind verbatim. It defines
what SP1 builds, the DDL and module contracts, the internal phasing, and the exit criteria.

---

## 1. Goal

Stand up the Stream-1 storage core and prove the full chain end-to-end on the one source
whose raw data is already on disk (`01_raw/pmc/oa_comm/` — 2 sample articles):

```
download → extract → stage → load → graph → materialize → enrich   (+ audit at every commit)
```

Everything SP2/SP3/SP4 builds against comes from here.

## 2. Environment prerequisite

PostgreSQL **19** reachable via `PG*` env vars — the operator's local install is
`localhost:5433`, superuser `postgres` / `postgres`. SP1 owns a scratch database
(`PGDATABASE=episteme`, create/drop tables freely) and a separate throwaway DB at
`TEST_PG_DSN` (`dbname=episteme_test`) for `pg`-marked tests. The real `NCBI_API_KEY` and
these connection values are written to the repo-root `./.env` (gitignored) when SP1-β
creates it — **never** into `.env.example` or any committed file. **Environment probed 2026-09-02:** PostgreSQL **19beta3** on `localhost:5433`.
- **SQL/PGQ present** — `CREATE PROPERTY GRAPH` parses. `graph_builder` uses the PGQ path
  as primary; the recursive-CTE path is retained only for portability/CI (`init_database.sh`
  still probes and records the result).
- **`vector` (pgvector) and `pg_search` NOT available** on this build. `extensions.sql`
  emits a NOTICE and continues; the `chunks` table is created **without** the `embedding` /
  `chunk_tsv` columns (Phase-1-dormant regardless). Add them via `migrate_database.sh` when
  the extensions are installed.
- **`pg_stat_statements` 1.13** available — enabled by `init_database.sh`.
- Only the `postgres` database exists; `init_database.sh` (run as superuser `postgres`)
  creates `episteme`, `episteme_test`, and the `episteme_app` role.

## 3. Internal phasing

SP1's writing-plans plan sequences two internal phases so the no-database work lands first
and keeps `pytest -q` green throughout:

- **SP1-α (no PostgreSQL):** the three renames + import-site fixes; merge upstream
  `data/pmc/extract.py` → `extract_pmc.py`; `sample_audit.py` (pure, filesystem-only);
  `article_schema.py` fills. Ends green with `pg` tests absent (none written yet).
- **SP1-β (PostgreSQL 19):** `db/`, `audit_trail.py`, `postgres_loader.py`, `load_articles.py`,
  `graph_builder.py`, `corpus_materializer.py`, `openmetadata_manifest.py`,
  `enrich_openmetadata.py`, the `scripts/data/pmc/*` + `_lib` + `init_database.sh` +
  seed `run_pipeline.sh`, and the proving slice. `pg`-marked tests skip cleanly without
  `TEST_PG_DSN`.

ADR-0001 and ADR-0002 are written at the **start of SP1-β**, before `db/schema.sql`.

---

## 4. File inventory

### 4.1 Renames (SP1-α, `git mv` + fill)

| From | To | Fill |
|---|---|---|
| `src/episteme/data/schema.py` | `article_schema.py` | add `SCHEMA_VERSION` changelog block; extend `SOURCES` to the roadmap §4.2 list; keep all helpers |
| `src/episteme/data/ops.py` | `checkpoint_markers.py` | add `load_success` / `graph_success` marker paths; no behaviour change |
| `src/episteme/data/writer.py` | `staging_writer.py` | rename `write_parquet_shard` path root to `02_processed/staging/<source>/`; keep JSONL fallback |

Import sites to update (`git grep 'episteme.data.\(schema\|ops\|writer\)'`):
`data/apollo/extract.py`, `data/pmc/extract.py`, `data/pubmed/extract.py`,
`data/europepmc/preprints/extract_europepmc_preprints.py`, `checkpoint_markers.py` itself.

### 4.2 PMC extract merge (SP1-α)

`data/pmc/extract_pmc.py` (currently a `NotImplementedError` stub) receives the real logic
from `data/pmc/extract.py` (the upstream file), adapted to: the renamed imports; the roadmap
§4.7 CLI (`--raw-dir --processed-dir --max-files [--force] [--workers]`); `article_schema`
row output via `staging_writer`; `checkpoint_markers` per input; `audit_trail.record` calls
left as no-op-safe hooks (real in SP1-β). Delete `data/pmc/extract.py`. Update the two
`data/pmc/extract.py` references (if any) + the `scripts/extract_pmc_oa_comm.sh` invocation.

### 4.3 New modules (SP1-β)

```
src/episteme/data/
  db/
    __init__.py
    schema.sql            # §5 — DDL per ADR-0002; PROPERTY GRAPH DDL attempted, guarded
    extensions.sql        # CREATE EXTENSION IF NOT EXISTS vector; pg_search;  (each guarded, non-fatal)
    connection.py         # psycopg (v3) connection + a small pool; DSN from config.get_settings().pg_dsn()
  audit_trail.py          # real: episteme._audit (caller's txn) + JSONL mirror, SHA-256 hash chain
  postgres_loader.py      # staging Parquet -> episteme.articles / episteme.id_map; delete-by-source_file + COPY, one txn per input file
  load_articles.py        # CLI: python -m episteme.data.load_articles --source <s> [--raw-dir ...] --processed-dir ... [--force --reason ...]
  graph_builder.py        # loaded rows + raw ref lists -> episteme.article_cites, episteme.article_mesh; CTE + guarded-PGQ query helpers
  corpus_materializer.py  # episteme.articles -> 03_corpus/pretrain/*.parquet; wires data/curate/deduplicate_corpus + decontaminate_benchmarks
  openmetadata_manifest.py# build the OM ingestion manifest (pure; jsonschema-validated)
  enrich_openmetadata.py  # CLI: (re)generate + validate the manifest for a source
```
(`sample_audit.py` is SP1-α — it is filesystem-only, reads `02_processed/staging/<source>/`,
writes the field-shape report; see §4.6.)

### 4.4 Scripts (SP1-β)

```
scripts/data/
  _lib/common.sh          # SP1 lands: log die require_env load_dotenv write_sync_stamp
                          #   (fetch helpers discover_manifest/size_match_skip/aria2_fetch/aws_sync are SP3)
  run_pipeline.sh         # seed: dispatch <source> <stage|all> for pmc only; stop-on-first-failure; --reason gate on --force
  db/
    init_database.sh      # psql -f schema.sql + extensions.sql against $PG*; probe SQL/PGQ; create the episteme schema
    migrate_database.sh   # stub: numbered migration runner (real use in SP2+)
  pmc/
    download_pmc.sh        # thin wrapper -> episteme.data.pmc.download_pmc  (module already in package)
    extract_pmc.sh         # -> python -m episteme.data.pmc.extract_pmc
    load_pmc.sh            # -> python -m episteme.data.load_articles --source pmc
    graph_pmc.sh           # -> python -m episteme.data.graph_builder --source pmc
    enrich_pmc.sh          # -> python -m episteme.data.enrich_openmetadata --source pmc
  materialize_corpus.sh    # -> python -m episteme.data.corpus_materializer
  verify_audit_trail.sh    # -> walks episteme._audit + the JSONL mirror, recomputes the hash chain
```

### 4.5 ADRs (SP1-β, before `schema.sql`)

`docs/adr/0001-hybrid-storage-architecture.md`, `docs/adr/0002-data-model-storage-and-partitioning.md`
— content per the 2026-09-01 spec §3.9 (Context / Decision / Consequences / Alternatives).
ADR-0002 is binding on `schema.sql`.

### 4.6 Tests

```
tests/data/
  test_article_schema.py       # SP1-α: license normalisation, extract_status thresholds, build_text, SOURCES membership
  test_checkpoint_markers.py   # SP1-α: marker write/skip/replace; new load_success/graph_success paths
  test_staging_writer.py       # SP1-α: rows -> parquet round-trip, staging path shape
  test_extract_pmc.py          # SP1-α: fixture PMC JATS XML -> article_schema rows, status rules
  test_sample_audit.py         # SP1-α: staging fixture -> field-shape report shape
  test_audit_trail.py          # SP1-β (pg): hash-chain integrity, append-only, tamper detection, JSONL mirror parity
  test_postgres_loader.py      # SP1-β (pg): load 2 fixture rows, idempotent delete-by-source_file replace
  test_graph_builder.py        # SP1-β (pg): article_mesh population from fixture; CTE query returns expected neighbours
  test_corpus_materializer.py  # SP1-β (pg): filter correctness (subset/status), dedup + decontam applied, parquet round-trip
  test_openmetadata_manifest.py# SP1-β: manifest is jsonschema-valid
```
`pg`-marked tests skip without `TEST_PG_DSN`. SP1-α leaves `pytest -q` green; SP1-β adds the
`pg` set (green where `TEST_PG_DSN` is set, skipped otherwise).

---

## 5. `db/schema.sql` — DDL (per ADR-0002)

Schema `episteme`. All append-only tables `fillfactor = 100`. `pg_stat_statements` enabled.

- **`episteme.articles`** — narrow hot columns (identifiers, `source`, `license`, `subset`,
  `extract_status`, `extract_notes`, `year`, `retrieved_at`, `content_hash`, `pmc_version`,
  `is_manuscript`, `is_historical_ocr`, `pdf_url`, `is_retracted`).
  `PARTITION BY LIST (source)`; each source sub-partitioned `PARTITION BY RANGE (year)`
  (buckets: `0` = unknown, `(,1990)`, then 5-year ranges to `[2025,2030)`, `[2030,)`).
  SP1 creates the `pmc` list-partition + its year sub-partitions; other sources' partitions
  are added by their sub-plans.
- **`episteme.article_body`** — `article_id` PK/FK → `articles`, `title`, `abstract`,
  `body_text`, `text`. `ALTER … SET (toast_compression = zstd)` on `body_text` / `text`.
  Mirrors `articles` partitioning for partition-wise joins.
- **`episteme.id_map`** — `pmid`, `pmcid`, `doi` (+ `source_file`, `retrieved_at`); unpartitioned.
- **`episteme.article_cites`** — `src_pmid`, `dst_pmid`, `source_file`; `PARTITION BY HASH (src_pmid)`, 8 buckets.
- **`episteme.article_mesh`** — `pmid`, `descriptor_ui`, `descriptor_name`, `major_topic bool`,
  `qualifiers text[]`, `source_file`; `PARTITION BY HASH (pmid)`, 8 buckets.
- **`episteme.chunks`** — Phase-1 scaffold: `article_id`, `chunk_no`, `chunk_text`,
  `embedding vector` (if `pgvector`), `chunk_tsv` (if `pg_search`); `PARTITION BY HASH (article_id)`, 8 buckets. Created empty; no writer in Phase 0.
- **`episteme._runs`** — `run_id`, `source`, `config jsonb`, `totals jsonb`, `started_at`. Unpartitioned.
- **`episteme._lineage`** — `source`, `source_file`, `run_id`, `input_content_hash`, `rows_inserted`,
  `rows_deleted`, `loaded_at`. Unpartitioned.
- **`episteme._audit`** — `seq bigserial`, `recorded_at timestamptz`, `actor`, `host`, `pid`,
  `run_id`, `code_version`, `event_type`, `object`, `input_content_hash`, `rows_affected`,
  `old_value jsonb`, `new_value jsonb`, `reason`, `prev_hash`, `record_hash`.
  `PARTITION BY RANGE (recorded_at)`, **monthly**; SP1 creates the current + next month.
  `GRANT INSERT, SELECT ON episteme._audit TO episteme_app` — **no `UPDATE` / `DELETE`**.

**Roles.** `postgres/postgres` (localhost:5433) is **setup only** — `init_database.sh` runs
the DDL as `postgres`, then creates a non-superuser role **`episteme_app`** (password from
`EPISTEME_DB_PASSWORD` env) with full DML on the `episteme` schema **except** `UPDATE`/`DELETE`
on `_audit`. The pipeline connects as `episteme_app` (`.env` `PGUSER=episteme_app`), so the
append-only `_audit` guarantee is actually enforced (a superuser bypasses grants). `TEST_PG_DSN`
also uses `episteme_app`.
- **Property graph** — `CREATE PROPERTY GRAPH episteme_graph VERTEX TABLES (articles …)
  EDGE TABLES (article_cites …, article_mesh …)`. SQL/PGQ is present on the target
  (19beta3), so this is expected to succeed; `init_database.sh` still wraps it so a parse
  failure on some other build is logged, not fatal.

`extensions.sql`: `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` (available), and
`vector` / `pg_search` each in a `DO $$ … EXCEPTION WHEN undefined_file THEN RAISE NOTICE
… $$;` guard (both absent on 19beta3 → NOTICE, non-fatal). `chunks` (§ above) is created
without `embedding` / `chunk_tsv` until those extensions land, via `migrate_database.sh`.

---

## 6. Module contracts (beyond roadmap §4)

- **`audit_trail.record(event_type, *, conn=None, **fields)`** — when `conn` is passed, the
  `_audit` INSERT runs in that transaction (contemporaneous). Computes `record_hash =
  sha256(canonical_json({… , prev_hash}))`; `prev_hash` = the previous row's `record_hash`
  (or 64 zeros for `seq = 1`). Also appends the same record to
  `02_processed/_ops/_audit/audit-YYYYMMDD.jsonl`. `actor` from `config.require_actor()`.
- **`postgres_loader.load_source_file(conn, source, staging_parquet_path)`** — one txn:
  `DELETE FROM episteme.articles WHERE source_file = $1` (+ `article_body`), `COPY` the new
  rows, write `_lineage`, `audit_trail.record("load_replace"/"load_commit", conn=conn, …)`,
  `checkpoint_markers.mark_success`. `id_mappings` source routes to `episteme.id_map` instead.
- **`graph_builder`** — `--source pmc`: reads loaded `articles` + the raw JATS ref lists /
  MeSH headings from `01_raw`, upserts `article_cites` / `article_mesh` per `source_file`
  (delete-by-`source_file` + insert), audits `graph_commit`. Exposes
  `neighbours(conn, pmid, hops=1)` with a CTE implementation and a PGQ implementation behind
  `settings`-driven selection.
- **`corpus_materializer.materialize(conn, out_root)`** — `SELECT` from `articles` ⨝
  `article_body` where `subset='commercial' AND extract_status='ok'`; run
  `deduplicate_corpus` (MinHash LSH on `text`, drop near-dups) then
  `decontaminate_benchmarks` (13-gram vs the eval sets) over the stream; write Hive-layout
  `03_corpus/pretrain/source=<s>/year=<y>/part-*.parquet` (zstd:3, 128 MB row groups,
  `content_hash`-sorted); audit `corpus_materialize`.
- **`sample_audit.build_report(staging_dir, source)`** — → `_ops/<source>/field_shape_report.md`
  + `.json`: per-column non-null %, distinct-count, sample values; licence strings seen;
  `extract_status` histogram; flagged surprises.

---

## 7. Exit criteria

1. `pytest -q` green (SP1-α set always; SP1-β `pg` set green with `TEST_PG_DSN`, else skipped).
2. Fresh venv: `pip install -e ".[data]"` (now also pulls `psycopg[binary]`, `pgvector`,
   `jsonschema`) + `python -c "import episteme.data.db.connection, episteme.data.load_articles,
   episteme.data.graph_builder, episteme.data.corpus_materializer"`.
3. `scripts/data/db/init_database.sh` against the PG 19 instance → `episteme` schema present,
   all tables + partitions per §5, `_audit` grants correct, SQL/PGQ status logged.
4. `scripts/data/run_pipeline.sh pmc all --max-files 2 --reason "SP1 proving slice"` on the
   on-disk `01_raw/pmc/oa_comm/` sample →
   - `02_processed/staging/pmc/*.parquet` written;
   - `episteme.articles` has 2 rows, correct `source`/`subset`/`license`/`extract_status`;
     `episteme.article_body` has the matching 2;
   - `episteme._lineage` row per input file;
   - `episteme._audit`: `run_start` + `load_commit`×2 + `graph_commit` + `corpus_materialize`
     + `run_end`, valid `prev_hash`/`record_hash` chain, JSONL mirror parity;
   - `verify_audit_trail.sh` → chain intact; app role cannot `UPDATE`/`DELETE` `_audit`;
   - `episteme.article_mesh` populated from the sample's MeSH headings;
   - `03_corpus/pretrain/source=pmc/year=*/part-*.parquet` shard exists and reads back;
   - `_ops/pmc/catalog/pmc.openmetadata.json` validates.
5. **PMC field-shape report** (`_ops/pmc/field_shape_report.md`) produced; any
   `article_schema` deltas from it applied and `SCHEMA_VERSION` bumped; `docs/09` §3 table
   noted for SP5.
6. `git log --follow` shows history preserved across the three renames + the PMC extract move.

---

## 8. Open items (carried from the roadmap; SP1 resolves #3)

- **#3 PMC-as-slice** — confirmed viable iff `01_raw/pmc/oa_comm/` still has the 2 sample
  articles at SP1 kickoff (checked in task 1). If gone, re-fetch with
  `scripts/data/pmc/download_pmc.sh … --max-files 2` first.
- SQL/PGQ presence on the installed PG 19 — determined by `init_database.sh`; no SP1 blocker.
- `pgvector` / `pg_search` availability — `extensions.sql` degrades to a NOTICE; `chunks`
  table still created (embedding/tsv columns conditional).

## 9. Document control

| Version | Date | Notes |
|---|---|---|
| v1 | 2026-09-02 | Initial SP1 spec from the Phase-0 roadmap. |
